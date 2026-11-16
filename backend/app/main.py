import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agents, auth, config, db, evals, llm, meter, orchestrator
from .auth import User, admin_user, current_user
from .config import OUTPUT_DIR, UPLOAD_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

DATASET_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".json"}
DOCUMENT_EXT = {".pdf", ".txt", ".md"}
# fallback token estimates per depth until there are completed runs to average
DEFAULT_ESTIMATES = {"quick": 45_000, "standard": 80_000, "deep": 130_000}
MIN_TOKENS_TO_START = 5_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.ensure_admin()
    auth.purge_expired()
    orchestrator.mark_interrupted()
    await orchestrator.init()
    yield
    await orchestrator.shutdown()


app = FastAPI(title="ResearchX", lifespan=lifespan)
log = logging.getLogger("researchx.api")


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Turn crashes into a readable JSON error for the UI (the full traceback goes to the server log)."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Server error: {type(exc).__name__}: {exc}"})


@app.get("/api/health")
def health():
    return {"ok": True, "models": llm.available_models(),
            "search": "tavily" if config.TAVILY_API_KEY else "ddgs + wikipedia",
            "embeddings": config.EMBEDDING_MODEL if config.GEMINI_API_KEY else "local-hashed"}


# ======================================================================= auth
_failed_logins: dict[str, list[float]] = defaultdict(list)


def _set_session(response: Response, user: User) -> None:
    token, expires = auth.new_session(user)
    response.set_cookie(auth.COOKIE, token, max_age=int(expires - time.time()), httponly=True,
                        samesite="lax", secure=config.COOKIE_SECURE, path="/")


@app.get("/api/auth/config")
def auth_config():
    s = auth.settings()
    return {"guest_enabled": s["guest_enabled"], "signup_enabled": s["signup_enabled"],
            "guest_tokens": s["guest_tokens"], "default_user_tokens": s["default_user_tokens"],
            "guest_hours": config.GUEST_HOURS}


@app.post("/api/auth/login")
def login(request: Request, response: Response, username: str = Body(...), password: str = Body(...)):
    key = f"{auth.client_ip(request)}:{username.lower()}"
    recent = [t for t in _failed_logins[key] if t > time.time() - 600]
    _failed_logins[key] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many failed attempts — try again in 10 minutes")
    rows = db.query("SELECT * FROM users WHERE lower(username)=lower(?) AND role!='guest'", (username.strip(),))
    if not rows or not auth.verify_password(password, rows[0]["password_hash"]):
        recent.append(time.time())
        raise HTTPException(401, "Wrong username or password")
    user = auth.get_user(rows[0]["id"])
    if user.disabled:
        raise HTTPException(403, "This account is disabled — contact the admin")
    _failed_logins.pop(key, None)
    _set_session(response, user)
    return auth.public_user(user)


@app.post("/api/auth/signup")
def signup(response: Response, username: str = Body(...), password: str = Body(...)):
    s = auth.settings()
    if not s["signup_enabled"]:
        raise HTTPException(403, "Sign-ups are closed — ask the admin for an account")
    available = max(auth.pool_status()["available"], 0)
    user = auth.create_user(username.strip(), password, "user", min(s["default_user_tokens"], available))
    _set_session(response, user)
    return auth.public_user(user)


@app.post("/api/auth/guest")
def guest(request: Request, response: Response):
    s = auth.settings()
    if not s["guest_enabled"]:
        raise HTTPException(403, "Guest mode is turned off")
    ip = auth.client_ip(request)
    if auth.guests_today_from(ip) >= config.GUESTS_PER_IP_PER_DAY:
        raise HTTPException(429, "Guest limit reached for today — create an account instead")
    if auth.pool_status()["available"] < s["guest_tokens"]:
        raise HTTPException(503, "The vault has no tokens left for guests right now")
    user = auth.create_user(f"guest-{uuid.uuid4().hex[:6]}", None, "guest", s["guest_tokens"],
                            expires_at=time.time() + config.GUEST_HOURS * 3600, ip=ip)
    _set_session(response, user)
    return auth.public_user(user)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return auth.public_user(user)


@app.post("/api/auth/upgrade")
def upgrade_guest(username: str = Body(...), password: str = Body(...), user: User = Depends(current_user)):
    """Turn a guest into a full account, keeping its runs. Unused guest tokens carry over."""
    if not user.is_guest:
        raise HTTPException(400, "Only guest sessions can be upgraded")
    s = auth.settings()
    if not s["signup_enabled"]:
        raise HTTPException(403, "Sign-ups are closed — ask the admin for an account")
    if not auth.USERNAME_RE.match(username) or len(password) < 8:
        raise HTTPException(400, "Username 3-32 chars (letters, digits, _ . -) and password of 8+ characters")
    if db.query("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)):
        raise HTTPException(409, "That username is taken")
    db.execute("UPDATE users SET username=?, password_hash=?, role='user', expires_at=NULL WHERE id=?",
               (username, auth.hash_password(password), user.id))
    upgraded = auth.get_user(user.id)
    target = max(upgraded.token_limit or 0, upgraded.tokens_used + s["default_user_tokens"])
    try:
        auth.grant(upgraded, target)
    except HTTPException:
        pass  # pool can't cover the sign-up bonus; keep the guest allotment
    db.execute("UPDATE sessions SET expires_at=? WHERE user_id=?", (time.time() + config.SESSION_DAYS * 86400, user.id))
    return auth.public_user(auth.get_user(user.id))


@app.post("/api/auth/password")
def change_password(current: str = Body(...), new: str = Body(...), user: User = Depends(current_user)):
    row = db.query("SELECT password_hash FROM users WHERE id=?", (user.id,))[0]
    if not auth.verify_password(current, row["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    if len(new) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(new), user.id))
    return {"ok": True}


@app.get("/api/me/usage")
def my_usage(user: User = Depends(current_user)):
    return {
        "by_day": db.query("SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(input_tokens + output_tokens) AS tokens "
                           "FROM token_ledger WHERE user_id=? AND ts > ? GROUP BY day ORDER BY day",
                           (user.id, time.time() - 14 * 86400)),
        "by_agent": db.query("SELECT agent, SUM(input_tokens + output_tokens) AS tokens, COUNT(*) AS calls "
                             "FROM token_ledger WHERE user_id=? GROUP BY agent ORDER BY tokens DESC", (user.id,)),
    }


# ======================================================================= runs
def _estimates() -> dict:
    """Average tokens of finished runs per depth (falls back to defaults before there is data)."""
    rows = db.query("SELECT state, tokens_used FROM runs WHERE status='completed' AND tokens_used > 0 "
                    "ORDER BY created_at DESC LIMIT 60")
    seen: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        depth = json.loads(r["state"]).get("options", {}).get("depth")
        seen[depth].append(r["tokens_used"])
    return {d: (round(sum(seen[d]) / len(seen[d]), -3) if seen[d] else v) for d, v in DEFAULT_ESTIMATES.items()}


@app.get("/api/estimates")
def estimates(user: User = Depends(current_user)):
    s = auth.settings()
    allowed = auth.DEPTH_ORDER[: auth.DEPTH_ORDER.index(s["guest_max_depth"]) + 1] if user.is_guest else auth.DEPTH_ORDER
    return {"tokens": _estimates(), "allowed_depths": allowed,
            "uploads_allowed": (not user.is_guest) or s["guest_uploads"]}


def _owned_run(run_id: str, user: User) -> dict:
    rec = db.get_run(run_id)
    if not rec or (rec["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "Run not found")
    return rec


@app.post("/api/runs")
async def create_run(
    question: str = Form(...),
    depth: str = Form("standard"),
    max_revisions: int = Form(config.MAX_REVISIONS),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
):
    question = question.strip()
    if len(question) < 5:
        raise HTTPException(400, "Question is too short")
    if depth not in auth.DEPTH_ORDER:
        raise HTTPException(400, "depth must be quick|standard|deep")
    s = auth.settings()
    if user.is_guest and auth.DEPTH_ORDER.index(depth) > auth.DEPTH_ORDER.index(s["guest_max_depth"]):
        raise HTTPException(403, f"Guests can run up to '{s['guest_max_depth']}' depth — create an account for more")
    if user.is_guest and not s["guest_uploads"] and any(f.filename for f in files):
        raise HTTPException(403, "File uploads are disabled for guests")
    if not any(llm.chain_for(r) for r in config.ROLE_MODELS):
        raise HTTPException(400, "No LLM provider configured. Add GROQ_API_KEY / GEMINI_API_KEY to backend/.env")
    left = auth.remaining(user)
    if left is not None and left < MIN_TOKENS_TO_START:
        raise HTTPException(402, f"Only {left:,} tokens left — ask the admin for more")

    documents, datasets = [], []
    batch_dir = UPLOAD_DIR / uuid.uuid4().hex[:12]
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in DATASET_EXT | DOCUMENT_EXT:
            raise HTTPException(400, f"Unsupported file type: {f.filename}")
        batch_dir.mkdir(parents=True, exist_ok=True)
        dest = batch_dir / Path(f.filename).name
        dest.write_bytes(await f.read())
        (datasets if ext in DATASET_EXT else documents).append({"name": dest.name, "path": str(dest)})

    state = orchestrator.new_state(question, depth, max(0, min(max_revisions, 4)), documents, datasets)
    run_id = db.create_run(question, state, user.id)
    orchestrator.start(run_id)
    return {"id": run_id}


@app.get("/api/runs")
def list_runs(all: bool = False, user: User = Depends(current_user)):
    return db.list_runs(None if (all and user.is_admin) else user.id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user)):
    rec = _owned_run(run_id, user)
    rec["active"] = orchestrator.is_active(run_id)
    rec["tokens_used"] = db.query("SELECT tokens_used FROM runs WHERE id=?", (run_id,))[0]["tokens_used"]
    return rec


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str, user: User = Depends(current_user)):
    rec = _owned_run(run_id, user)
    if rec["status"] == "completed" or orchestrator.is_active(run_id):
        raise HTTPException(400, f"Run is {rec['status']}")
    left = auth.remaining(auth.get_user(rec["user_id"]))
    if left is not None and left <= 0:
        raise HTTPException(402, "No tokens left — ask the admin for more, then resume")
    db.add_event(run_id, "resumed", "orchestrator", f"Resuming from '{rec['state']['next_node']}'")
    orchestrator.start(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: User = Depends(current_user)):
    _owned_run(run_id, user)
    return {"ok": orchestrator.cancel(run_id)}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, user: User = Depends(current_user)):
    _owned_run(run_id, user)
    await orchestrator.forget(run_id)
    db.delete_run(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/evaluate")
async def evaluate_run(run_id: str, user: User = Depends(current_user)):
    rec = _owned_run(run_id, user)
    if orchestrator.is_active(run_id):
        raise HTTPException(400, "Run is still active")
    if not (rec["state"].get("final_report") or rec["state"].get("drafts")):
        raise HTTPException(400, "Nothing to evaluate yet")
    orchestrator.start_evaluation(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/ask")
async def ask_run(run_id: str, question: str = Body(..., embed=True), user: User = Depends(current_user)):
    rec = _owned_run(run_id, user)
    question = question.strip()
    if len(question) < 3:
        raise HTTPException(400, "Question is too short")
    if orchestrator.is_active(run_id):
        raise HTTPException(400, "Wait for the run to finish")
    if not rec["state"]["sources"]:
        raise HTTPException(400, "This run has no indexed sources")
    token = meter.scope.set(meter.Scope(run_id=run_id, user_id=rec["user_id"]))
    try:
        meter.check_budget()
        emit = orchestrator._emitter(run_id)
        qa = await agents.answer_followup(agents.Ctx(run_id=run_id, emit=emit), rec["state"], question)
    except meter.BudgetExceeded as e:
        raise HTTPException(402, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    finally:
        meter.scope.reset(token)
        meter.live_clear(run_id)
    latest = db.get_run(run_id)
    state = latest["state"]
    state.setdefault("qa", []).append(qa)
    db.save_run(run_id, latest["status"], state)
    return qa


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, after: int = 0, user: User = Depends(current_user)):
    _owned_run(run_id, user)

    async def gen():
        last, idle, live_v = after, 0, -1
        while True:
            events = db.get_events(run_id, last)
            for ev in events:
                last = ev["id"]
                yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
            live = meter.live_get(run_id)
            if live and live["v"] != live_v:
                live_v = live["v"]
                yield f"event: live\ndata: {json.dumps(live)}\n\n"
            if events:
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": keepalive\n\n"
                if not orchestrator.is_active(run_id) and idle > 4:
                    yield "event: end\ndata: {}\n\n"
                    return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str, user: User = Depends(current_user)):
    _owned_run(run_id, user)
    path = (OUTPUT_DIR / run_id / name).resolve()
    if not path.is_relative_to(OUTPUT_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# ======================================================================= evaluations
METRICS = ["overall", "faithfulness", "context_precision", "ndcg", "mrr", "answer_relevance",
           "subquestion_coverage", "citation_precision", "citation_coverage", "hallucination_rate",
           "ranker_agreement", "best_critic", "tokens", "wall_seconds", "fallback_rate"]


@app.get("/api/evals")
def eval_dashboard(all: bool = False, user: User = Depends(current_user)):
    where, params = ("", ()) if (all and user.is_admin) else ("WHERE r.user_id=?", (user.id,))
    runs = db.query(f"SELECT r.id, r.question, r.created_at, r.state, r.status, u.username FROM runs r "
                    f"LEFT JOIN users u ON u.id=r.user_id {where} ORDER BY r.created_at DESC LIMIT 200", params)
    rows, pending = [], []
    agent_tot: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tokens": 0, "latency_ms": 0, "fallbacks": 0})
    for r in runs:
        state = json.loads(r["state"])
        e = state.get("evaluation")
        if e and "overall" in e:
            rows.append(evals.summary_row(r, e))
            for name, a in e["agents"]["per_agent"].items():
                t = agent_tot[name]
                t["calls"] += a["calls"]
                t["tokens"] += a["input_tokens"] + a["output_tokens"]
                t["latency_ms"] += a["latency_ms"]
                t["fallbacks"] += a["fallbacks"]
        elif r["status"] == "completed":
            pending.append({"id": r["id"], "question": r["question"]})
    averages = {}
    for m in METRICS:
        vals = [x[m] for x in rows if isinstance(x.get(m), (int, float))]
        averages[m] = round(sum(vals) / len(vals), 3) if vals else None
    agents_summary = {k: {**v, "avg_latency_s": round(v["latency_ms"] / v["calls"] / 1000, 2) if v["calls"] else 0,
                          "fallback_rate": round(v["fallbacks"] / v["calls"], 3) if v["calls"] else 0}
                      for k, v in agent_tot.items()}
    return {"runs": rows, "averages": averages, "agents": agents_summary, "unevaluated": pending}


# ======================================================================= admin
@app.get("/api/admin/overview")
def admin_overview(_: User = Depends(admin_user)):
    now = time.time()
    return {
        "pool": auth.pool_status(),
        "settings": auth.settings(),
        "users": db.query("SELECT role, COUNT(*) AS n FROM users WHERE disabled=0 AND (expires_at IS NULL OR expires_at > ?) "
                          "GROUP BY role", (now,)),
        "runs": db.query("SELECT status, COUNT(*) AS n FROM runs GROUP BY status"),
        "by_day": db.query("SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(input_tokens + output_tokens) AS tokens, "
                           "COUNT(*) AS calls FROM token_ledger WHERE ts > ? GROUP BY day ORDER BY day", (now - 14 * 86400,)),
        "by_model": db.query("SELECT model, SUM(input_tokens + output_tokens) AS tokens, COUNT(*) AS calls, "
                             "ROUND(AVG(latency_ms)) AS avg_ms FROM token_ledger GROUP BY model ORDER BY tokens DESC"),
        "by_agent": db.query("SELECT agent, SUM(input_tokens + output_tokens) AS tokens, COUNT(*) AS calls "
                             "FROM token_ledger GROUP BY agent ORDER BY tokens DESC"),
    }


@app.get("/api/admin/users")
def admin_users(_: User = Depends(admin_user)):
    rows = db.query(
        "SELECT u.id, u.username, u.role, u.token_limit, u.tokens_used, u.disabled, u.created_at, u.expires_at, "
        "u.last_seen, (SELECT COUNT(*) FROM runs r WHERE r.user_id=u.id) AS runs FROM users u "
        "ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'user' THEN 1 ELSE 2 END, u.created_at DESC")
    now = time.time()
    for r in rows:
        r["expired"] = bool(r["expires_at"] and r["expires_at"] < now)
    return rows


@app.post("/api/admin/users")
def admin_create_user(username: str = Body(...), password: str = Body(...), role: str = Body("user"),
                      token_limit: int = Body(0), _: User = Depends(admin_user)):
    if role not in ("user", "admin"):
        raise HTTPException(400, "role must be user or admin")
    user = auth.create_user(username.strip(), password, role, None if role == "admin" else 0)
    if role != "admin" and token_limit:
        try:
            auth.grant(user, token_limit)
        except HTTPException:
            db.execute("DELETE FROM users WHERE id=?", (user.id,))
            raise
    return auth.public_user(auth.get_user(user.id))


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, patch: dict = Body(...), me: User = Depends(admin_user)):
    user = auth.get_user(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == me.id and (patch.get("disabled") or patch.get("role", "admin") != "admin"):
        raise HTTPException(400, "You can't disable or demote yourself")
    if "role" in patch:
        if patch["role"] not in ("user", "admin") or user.is_guest:
            raise HTTPException(400, "Role must be user or admin (guests upgrade themselves)")
        db.execute("UPDATE users SET role=?, token_limit=? WHERE id=?",
                   (patch["role"], None if patch["role"] == "admin" else max(user.tokens_used, user.token_limit or 0),
                    user.id))
        user = auth.get_user(user_id)
    if "disabled" in patch:
        db.execute("UPDATE users SET disabled=? WHERE id=?", (int(bool(patch["disabled"])), user.id))
        if patch["disabled"]:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user.id,))
    if not user.is_admin:
        if patch.get("token_limit") is not None:
            auth.grant(auth.get_user(user_id), int(patch["token_limit"]))
        elif patch.get("add_tokens"):
            u = auth.get_user(user_id)
            auth.grant(u, max(0, (u.token_limit or 0) + int(patch["add_tokens"])))
    if patch.get("extend_hours") and user.is_guest:
        db.execute("UPDATE users SET expires_at=? WHERE id=?",
                   (max(user.expires_at or 0, time.time()) + float(patch["extend_hours"]) * 3600, user.id))
    if patch.get("password"):
        if len(patch["password"]) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(patch["password"]), user.id))
    return auth.public_user(auth.get_user(user_id))


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, me: User = Depends(admin_user)):
    if user_id == me.id:
        raise HTTPException(400, "You can't delete yourself")
    for r in db.query("SELECT id FROM runs WHERE user_id=?", (user_id,)):
        await orchestrator.forget(r["id"])
        db.delete_run(r["id"])
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_get_settings(_: User = Depends(admin_user)):
    return auth.settings()


@app.put("/api/admin/settings")
def admin_put_settings(patch: dict = Body(...), _: User = Depends(admin_user)):
    return {"settings": auth.update_settings(patch), "pool": auth.pool_status()}


@app.post("/api/admin/guests/purge")
def admin_purge_guests(_: User = Depends(admin_user)):
    return {"expired": auth.purge_expired()}


# Serve the built frontend (npm run build) when present, so one process runs everything.
_dist = config.BASE_DIR.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
