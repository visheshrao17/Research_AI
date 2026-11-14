"""LangGraph orchestration.

The agents form a cyclic StateGraph (plan -> research -> [analyze] -> write -> critique, with a
critique -> gap_research -> revise -> critique loop). Each node returns the name of the next node
in `next_node`, and a conditional edge routes on it. LangGraph checkpoints the state to SQLite
after every node (thread_id = run id), so an interrupted run (server restart, crash, cancel)
resumes from the exact node it stopped at. A copy of the state is also mirrored to the `runs`
table so the UI can read it cheaply.
"""
import asyncio
import copy
import logging
import time
import traceback
from typing import Any, TypedDict

import aiosqlite

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from . import db, evals, meter
from .agents import NODES, Ctx
from .config import DATA_DIR

log = logging.getLogger("researchx.orchestrator")
_tasks: dict[str, asyncio.Task] = {}
_graph = None
_checkpointer: AsyncSqliteSaver | None = None
CHECKPOINT_DB = DATA_DIR / "checkpoints.db"


class ResearchState(TypedDict, total=False):
    question: str
    options: dict
    uploads: dict
    plan: dict | None
    sources: list[dict]
    evidence: dict[str, list[dict]]
    analysis: dict | None
    drafts: list[dict]
    critiques: list[dict]
    iteration: int
    next_node: str
    final_report: str | None
    best_iteration: int | None
    models_used: dict
    evaluation: dict | None
    qa: list[dict]
    error: str | None


def new_state(question: str, depth: str, max_revisions: int, documents: list[dict], datasets: list[dict]) -> dict:
    return {
        "question": question,
        "options": {"depth": depth, "max_revisions": max_revisions},
        "uploads": {"documents": documents, "datasets": datasets},
        "plan": None, "sources": [], "evidence": {}, "analysis": None,
        "drafts": [], "critiques": [], "iteration": 0,
        "next_node": "plan", "final_report": None, "best_iteration": None,
        "models_used": {}, "evaluation": None, "qa": [], "error": None,
    }


# what the live panel says while each stage is running
STAGES = {
    "plan": ("planner", "Planning the research"),
    "research": ("researcher", "Researching the web"),
    "analyze": ("analyst", "Analyzing your datasets"),
    "write": ("writer", "Writing the first draft"),
    "critique": ("critic", "Reviewing the draft"),
    "gap_research": ("researcher", "Filling gaps the critic found"),
    "revise": ("reviser", "Revising the draft"),
    "finalize": ("orchestrator", "Assembling the final report"),
    "evaluate": ("evaluator", "Evaluating the run (RAG + agents)"),
}


def _emitter(run_id: str):
    async def emit(agent, message, data=None, type="log"):
        db.add_event(run_id, type, agent, message, data)
        if type in ("log", "source"):
            meter.live_set(run_id, agent=agent, detail=message)
    return emit


def _node(name: str, fn):
    async def run_node(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        run_id = config["configurable"]["thread_id"]
        emit = _emitter(run_id)
        s = copy.deepcopy(dict(state))
        s["next_node"] = name
        db.save_run(run_id, "running", s)
        agent, stage = STAGES.get(name, ("orchestrator", name))
        meter.live_set(run_id, node=name, stage=stage, stage_started=time.time(),
                       agent=agent, action=stage, detail="", stream="", model=None)
        await emit("orchestrator", f"▶ {name}", {"node": name}, type="node")
        s["next_node"] = await fn(Ctx(run_id=run_id, emit=emit), s)
        db.save_run(run_id, "running", s)
        return s
    return run_node


def _route(state: ResearchState) -> str:
    nxt = state.get("next_node", "plan")
    return END if nxt == "done" else nxt


def build_graph(checkpointer=None):
    g = StateGraph(ResearchState)
    for name, fn in NODES.items():
        g.add_node(name, _node(name, fn))
    targets = list(NODES) + [END]
    # Entry also routes on next_node, so a run without a checkpoint can still start mid-graph.
    g.add_conditional_edges(START, _route, targets)
    for name in NODES:
        g.add_conditional_edges(name, _route, targets)
    return g.compile(checkpointer=checkpointer)


async def init() -> None:
    global _graph, _checkpointer
    _checkpointer = AsyncSqliteSaver(await aiosqlite.connect(CHECKPOINT_DB))
    await _checkpointer.setup()
    _graph = build_graph(_checkpointer)


async def shutdown() -> None:
    for task in list(_tasks.values()):
        task.cancel()
    if _checkpointer:
        await _checkpointer.conn.close()


def _scope(run_id: str) -> None:
    meter.scope.set(meter.Scope(run_id=run_id, user_id=db.run_owner(run_id)))
    meter.live_set(run_id, active=True, run_tokens=(db.query("SELECT tokens_used FROM runs WHERE id=?", (run_id,))
                                                    or [{"tokens_used": 0}])[0]["tokens_used"])


async def _run(run_id: str) -> None:
    cfg: RunnableConfig = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
    emit = _emitter(run_id)
    _scope(run_id)  # runs inside its own task, so this ContextVar is private to the run
    try:
        snapshot = await _graph.aget_state(cfg)
        if snapshot.values and snapshot.next:
            graph_input = None  # resume from the last LangGraph checkpoint
        else:
            graph_input = db.get_run(run_id)["state"]
        final = await _graph.ainvoke(graph_input, cfg)
        db.save_run(run_id, "completed", final)
        await emit("orchestrator", "Research complete", type="done")
    except asyncio.CancelledError:
        db.save_run(run_id, "cancelled", db.get_run(run_id)["state"])
        db.add_event(run_id, "cancelled", "orchestrator", "Run cancelled — it can be resumed")
        raise
    except meter.BudgetExceeded as e:
        state = db.get_run(run_id)["state"]
        state["error"] = str(e)
        db.save_run(run_id, "paused", state)
        await emit("orchestrator", f"Paused at '{state['next_node']}': {e}", type="budget")
    except Exception as e:  # noqa: BLE001
        log.error("run %s failed: %s", run_id, traceback.format_exc())
        state = db.get_run(run_id)["state"]
        state["error"] = str(e)
        db.save_run(run_id, "failed", state)
        await emit("orchestrator", f"Failed at '{state['next_node']}': {e}", type="error")
    finally:
        _tasks.pop(run_id, None)
        meter.live_clear(run_id)


async def _evaluate(run_id: str) -> None:
    """Evaluate a run that finished before the evaluator existed (or re-evaluate one)."""
    emit = _emitter(run_id)
    _scope(run_id)
    try:
        meter.live_set(run_id, node="evaluate", stage=STAGES["evaluate"][1], agent="evaluator",
                       action=STAGES["evaluate"][1], stage_started=time.time())
        await emit("orchestrator", "▶ evaluate", {"node": "evaluate"}, type="node")
        state = db.get_run(run_id)["state"]
        state["evaluation"] = await evals.evaluate(Ctx(run_id=run_id, emit=emit), state)
        rec = db.get_run(run_id)
        db.save_run(run_id, rec["status"], {**rec["state"], "evaluation": state["evaluation"]})
        e = state["evaluation"]
        await emit("evaluator", f"Evaluation: overall {e['overall']}/100 · faithfulness {e['rag']['faithfulness']} · "
                                f"context precision {e['rag']['context_precision']}", {"evaluation": e}, type="evaluation")
        await emit("orchestrator", "Evaluation complete", type="done")
    except meter.BudgetExceeded as e:
        await emit("orchestrator", str(e), type="budget")
    except Exception as e:  # noqa: BLE001
        log.error("evaluation of %s failed: %s", run_id, traceback.format_exc())
        await emit("evaluator", f"Evaluation failed: {e}", type="error")
    finally:
        _tasks.pop(run_id, None)
        meter.live_clear(run_id)


def start_evaluation(run_id: str) -> None:
    if run_id not in _tasks:
        _tasks[run_id] = asyncio.create_task(_evaluate(run_id))


def start(run_id: str) -> None:
    if run_id in _tasks:
        return
    _tasks[run_id] = asyncio.create_task(_run(run_id))


def cancel(run_id: str) -> bool:
    task = _tasks.get(run_id)
    if task:
        task.cancel()
        return True
    return False


def is_active(run_id: str) -> bool:
    return run_id in _tasks


async def forget(run_id: str) -> None:
    cancel(run_id)
    if _checkpointer:
        await _checkpointer.adelete_thread(run_id)


def mark_interrupted() -> None:
    """Runs left 'running' by a previous process were interrupted; flag them as resumable."""
    for run_id in db.runs_with_status("running", "queued"):
        rec = db.get_run(run_id)
        db.save_run(run_id, "interrupted", rec["state"])
        db.add_event(run_id, "interrupted", "orchestrator", "Server restarted mid-run — resume to continue")
