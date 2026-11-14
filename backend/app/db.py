"""SQLite persistence: runs (with full orchestrator state), event log, RAG chunks, users/sessions and the token ledger."""
import json
import sqlite3
import threading
import time
import uuid

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        question TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        state TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        agent TEXT,
        message TEXT,
        data TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding BLOB
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_run ON chunks(run_id);
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        role TEXT NOT NULL,              -- admin | user | guest
        token_limit INTEGER,             -- NULL = no personal cap (admin); still bounded by the pool
        tokens_used INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        expires_at REAL,                 -- guests only
        created_ip TEXT,
        last_seen REAL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS token_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user_id TEXT,
        run_id TEXT,
        agent TEXT,
        role TEXT,
        model TEXT,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        latency_ms INTEGER,
        fallback INTEGER NOT NULL DEFAULT 0,
        estimated INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_ledger_run ON token_ledger(run_id);
    CREATE INDEX IF NOT EXISTS idx_ledger_user ON token_ledger(user_id, ts);
    """
)
# migrations for databases created before auth existed
_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(runs)")}
if "user_id" not in _cols:
    _conn.execute("ALTER TABLE runs ADD COLUMN user_id TEXT")
if "tokens_used" not in _cols:
    _conn.execute("ALTER TABLE runs ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, created_at)")
_conn.commit()


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------------- runs ----------------
def create_run(question: str, state: dict, user_id: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    now = time.time()
    _exec(
        "INSERT INTO runs (id, question, status, created_at, updated_at, state, user_id) VALUES (?,?,?,?,?,?,?)",
        (run_id, question, "queued", now, now, json.dumps(state), user_id),
    )
    return run_id


def save_run(run_id: str, status: str, state: dict) -> None:
    _exec(
        "UPDATE runs SET status=?, updated_at=?, state=? WHERE id=?",
        (status, time.time(), json.dumps(state), run_id),
    )


def get_run(run_id: str) -> dict | None:
    rows = _query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        return None
    r = rows[0]
    return {**dict(r), "state": json.loads(r["state"])}


def list_runs(user_id: str | None, limit: int = 50) -> list[dict]:
    """Runs owned by user_id, or every run (with owner name) when user_id is None (admin)."""
    where, params = ("WHERE r.user_id=?", (user_id,)) if user_id else ("", ())
    rows = _query(
        f"SELECT r.id, r.question, r.status, r.created_at, r.updated_at, r.tokens_used, r.user_id, u.username "
        f"FROM runs r LEFT JOIN users u ON u.id=r.user_id {where} ORDER BY r.created_at DESC LIMIT ?",
        (*params, limit),
    )
    return [dict(r) for r in rows]


def run_owner(run_id: str) -> str | None:
    rows = _query("SELECT user_id FROM runs WHERE id=?", (run_id,))
    return rows[0]["user_id"] if rows else None


def add_run_tokens(run_id: str, n: int) -> None:
    _exec("UPDATE runs SET tokens_used = tokens_used + ? WHERE id=?", (n, run_id))


def delete_run(run_id: str) -> None:
    # token_ledger rows are kept on purpose: tokens spent stay counted against the pool
    for table, col in (("runs", "id"), ("events", "run_id"), ("chunks", "run_id")):
        _exec(f"DELETE FROM {table} WHERE {col}=?", (run_id,))


def runs_with_status(*statuses: str) -> list[str]:
    marks = ",".join("?" * len(statuses))
    return [r["id"] for r in _query(f"SELECT id FROM runs WHERE status IN ({marks})", statuses)]


# ---------------- events ----------------
def add_event(run_id: str, type_: str, agent: str | None, message: str, data: dict | None = None) -> dict:
    ts = time.time()
    cur = _exec(
        "INSERT INTO events (run_id, ts, type, agent, message, data) VALUES (?,?,?,?,?,?)",
        (run_id, ts, type_, agent, message, json.dumps(data or {})),
    )
    return {"id": cur.lastrowid, "run_id": run_id, "ts": ts, "type": type_, "agent": agent,
            "message": message, "data": data or {}}


def get_events(run_id: str, after_id: int = 0) -> list[dict]:
    rows = _query("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
    return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]


# ---------------- chunks ----------------
def add_chunks(run_id: str, items: list[tuple[int, str, bytes | None]]) -> None:
    with _lock:
        _conn.executemany(
            "INSERT INTO chunks (run_id, source_id, text, embedding) VALUES (?,?,?,?)",
            [(run_id, sid, text, emb) for sid, text, emb in items],
        )
        _conn.commit()


def get_chunks(run_id: str) -> list[sqlite3.Row]:
    return _query("SELECT id, source_id, text, embedding FROM chunks WHERE run_id=? ORDER BY id", (run_id,))


# ---------------- generic helpers for auth / admin / evals ----------------
def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return _exec(sql, params)


def query(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in _query(sql, params)]


def get_setting(key: str, default=None):
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key: str, value) -> None:
    _exec("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, json.dumps(value)))
