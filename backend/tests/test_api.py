"""API smoke tests: auth, roles, token pool rules, stream tokens. No LLM keys or network needed."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # runs the lifespan: creates the admin, opens the graph checkpointer
        yield c


def login(client, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_requires_auth(client):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/admin/overview").status_code == 401


def test_signup_login_and_roles(client):
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={"username": "reader1", "password": "password123"})
    assert r.status_code == 200, r.text
    me = c.get("/api/auth/me").json()
    assert me["role"] == "user" and me["tokens_left"] == me["token_limit"] > 0
    assert c.get("/api/runs").json() == []
    assert c.get("/api/admin/overview").status_code == 403
    assert c.post("/api/auth/signup", json={"username": "reader1", "password": "password123"}).status_code == 409
    assert c.post("/api/auth/login", json={"username": "reader1", "password": "wrong-pass"}).status_code == 401


def test_guest_limits(client):
    g = TestClient(app)
    r = g.post("/api/auth/guest", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200 and r.json()["role"] == "guest"
    est = g.get("/api/estimates").json()
    assert "deep" not in est["allowed_depths"] and est["uploads_allowed"] is False
    r = g.post("/api/runs", data={"question": "A deep question for guests", "depth": "deep"})
    assert r.status_code == 403


def test_admin_pool_rules(client):
    admin = login(client, "admin", "admin-test-pass")
    users = admin.get("/api/admin/users").json()
    reader = next(u for u in users if u["username"] == "reader1")
    pool = admin.get("/api/admin/overview").json()["pool"]
    # can't grant more than the unallocated pool
    r = admin.patch(f"/api/admin/users/{reader['id']}", json={"token_limit": pool["pool"] * 10})
    assert r.status_code == 400
    r = admin.patch(f"/api/admin/users/{reader['id']}", json={"add_tokens": 10000})
    assert r.status_code == 200 and r.json()["token_limit"] == reader["token_limit"] + 10000
    # policy: closing sign-ups is enforced
    assert admin.put("/api/admin/settings", json={"signup_enabled": False}).status_code == 200
    r = TestClient(app).post("/api/auth/signup", json={"username": "reader2", "password": "password123"})
    assert r.status_code == 403
    admin.put("/api/admin/settings", json={"signup_enabled": True})


def test_stream_token(client):
    from app import auth, db, orchestrator

    admin = login(client, "admin", "admin-test-pass")
    admin_id = admin.get("/api/auth/me").json()["id"]
    run_id = db.create_run("token test question", orchestrator.new_state("token test question", "quick", 0, [], []),
                           admin_id)
    db.save_run(run_id, "completed", db.get_run(run_id)["state"])
    token = admin.get(f"/api/runs/{run_id}/stream-token").json()["token"]
    assert auth.user_from_stream_token(token, run_id).id == admin_id
    assert auth.user_from_stream_token(token, "other-run") is None
    assert auth.user_from_stream_token(token[:-1] + ("0" if token[-1] != "0" else "1"), run_id) is None
    anon = TestClient(app)
    assert anon.get(f"/api/runs/{run_id}/events?st=bad").status_code == 401
    with anon.stream("GET", f"/api/runs/{run_id}/events?st={token}") as r:
        assert r.status_code == 200
        assert "event: end" in "".join(r.iter_text())
