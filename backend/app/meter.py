"""Per-run token metering and the live "what is the agent doing right now" state.

The orchestrator sets `scope` (a ContextVar) when it starts a run task, so every LLM call made anywhere
inside that run is charged to the right user and run without threading ids through every function.

`live` is an in-memory snapshot per run (current agent, action, detail, model, streamed output, token
counters). The SSE endpoint pushes it to the browser whenever its version changes. It is deliberately
not persisted: it describes the present, the event log describes the past.
"""
import time
from contextvars import ContextVar
from dataclasses import dataclass

from . import auth, db


class BudgetExceeded(Exception):
    pass


@dataclass
class Scope:
    run_id: str
    user_id: str


scope: ContextVar[Scope | None] = ContextVar("researchx_scope", default=None)

# ---------------- live state ----------------
_live: dict[str, dict] = {}
STREAM_TAIL = 1400  # chars of streamed model output kept for the live preview


def live_get(run_id: str) -> dict | None:
    return _live.get(run_id)


def live_set(run_id: str | None = None, **fields) -> None:
    run_id = run_id or (scope.get().run_id if scope.get() else None)
    if not run_id:
        return
    cur = _live.setdefault(run_id, {"v": 0, "run_id": run_id})
    if "action" in fields and fields["action"] != cur.get("action"):
        cur["action_started"] = time.time()
        fields.setdefault("detail", "")
        fields.setdefault("stream", "")
        fields.setdefault("stream_chars", 0)
    cur.update(fields)
    cur["v"] += 1
    cur["ts"] = time.time()


def live_stream(text: str) -> None:
    sc = scope.get()
    if not sc:
        return
    cur = _live.get(sc.run_id)
    if cur is None:
        return
    cur["stream"] = text[-STREAM_TAIL:]
    cur["stream_chars"] = len(text)
    cur["v"] += 1


def live_clear(run_id: str) -> None:
    _live.pop(run_id, None)


# ---------------- budget ----------------
def check_budget() -> None:
    sc = scope.get()
    if not sc:
        return
    user = auth.get_user(sc.user_id)
    if user is None:
        raise BudgetExceeded("The account that owns this run no longer exists")
    left = auth.remaining(user)
    if left is not None and left <= 0:
        raise BudgetExceeded("Token budget exhausted — ask the admin for more tokens, then resume this run")


def record(agent: str, role: str, model: str, input_tokens: int, output_tokens: int,
           latency_ms: int, fallback: bool, estimated: bool) -> dict:
    """Write one LLM call to the ledger and charge it to the scoped user + run."""
    sc = scope.get()
    total = input_tokens + output_tokens
    db.execute(
        "INSERT INTO token_ledger (ts, user_id, run_id, agent, role, model, input_tokens, output_tokens, "
        "latency_ms, fallback, estimated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), sc.user_id if sc else None, sc.run_id if sc else None, agent, role, model,
         input_tokens, output_tokens, latency_ms, int(fallback), int(estimated)),
    )
    info = {"tokens": total}
    if sc:
        db.execute("UPDATE users SET tokens_used = tokens_used + ? WHERE id=?", (total, sc.user_id))
        db.add_run_tokens(sc.run_id, total)
        user = auth.get_user(sc.user_id)
        run_total = db.query("SELECT tokens_used FROM runs WHERE id=?", (sc.run_id,))
        info["run_tokens"] = run_total[0]["tokens_used"] if run_total else total
        info["tokens_left"] = auth.remaining(user) if user else 0
        live_set(sc.run_id, run_tokens=info["run_tokens"], tokens_left=info["tokens_left"])
    return info
