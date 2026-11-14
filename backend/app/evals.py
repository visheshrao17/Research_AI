"""Evaluation metrics for the RAG pipeline and for each agent.

Runs as the last graph node (or on demand for older runs). Mixes deterministic metrics with three
LLM-as-judge calls made by the `judge` role, which defaults to a different model family than the writer
so the grader isn't grading its own work.

RAG
  context_precision   share of retrieved chunks the judge rates relevant to their query (graded 0/1/2)
  ndcg / mrr          ranking quality of the retrieved chunks, using those graded labels
  faithfulness        share of sampled cited claims supported by the cited source (partial = 0.5)
  hallucination_rate  share of sampled claims judged unsupported or contradicted
  answer_relevance    how directly the report answers the question (0-1, from a 1-10 rating)
  subquestion_coverage share of the planner's sub-questions the report fully answers (partial = 0.5)
  citation_precision  citations that point to a real source / all citations
  ranker_agreement    overlap of BM25 top-k and dense top-k (low = the two rankers are complementary)

Agents: per-agent calls, tokens, latency and fallback rate from the token ledger; per-stage wall time
from the event log; plus planner query diversity, researcher yield, and critic -> reviser improvement.
"""
import math
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from langchain_core.prompts import ChatPromptTemplate

from . import db, llm, rag

MAX_CLAIMS = 12
CHUNK_CHARS = 450

CONTEXT_PROMPT = ChatPromptTemplate.from_template("""You are grading a retrieval system. For each QUERY below, rate every
retrieved CHUNK for how useful it is for answering that query:
2 = directly relevant (contains facts that answer it), 1 = partially/tangentially relevant, 0 = irrelevant.

Return JSON: {{"grades": [{{"query": <query number>, "scores": [<one integer per chunk, in order>]}}]}}

{blocks}""")

FAITH_PROMPT = ChatPromptTemplate.from_template("""You are a strict fact-checker. For each CLAIM, decide whether the
EVIDENCE excerpts from the source(s) it cites support it.
- "supported": the evidence states it (paraphrase is fine)
- "partial": some of it is supported, some is not, or numbers/details differ slightly
- "unsupported": the evidence does not say this
- "contradicted": the evidence says otherwise

Return JSON: {{"verdicts": [{{"claim": <claim number>, "verdict": "supported|partial|unsupported|contradicted",
"reason": "<10 words max>"}}]}}

{blocks}""")

ANSWER_PROMPT = ChatPromptTemplate.from_template("""You are evaluating a research report.

QUESTION: {question}

SUB-QUESTIONS the report was planned to answer:
{subqs}

REPORT:
{report}

Rate answer_relevance 1-10: does the report directly and specifically answer the QUESTION (10), or drift,
pad, or stay generic (low)? For each sub-question say whether the report answers it fully, partially, or not at all.

Return JSON: {{"answer_relevance": n, "coverage": [{{"subquestion": <number>, "status": "full|partial|missing"}}],
"rationale": "<one sentence>"}}""")


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


# ---------------------------------------------------------------- RAG: context relevance
def _primary_queries(state: dict) -> list[str]:
    return [q for q in state.get("evidence", {}) if not q.startswith("gap")]


async def context_relevance(ctx, state: dict) -> dict:
    queries = _primary_queries(state)
    if not queries:
        return {"context_precision": None, "ndcg": None, "mrr": None, "per_query": []}
    blocks = []
    for qi, q in enumerate(queries, 1):
        hits = state["evidence"][q]
        chunks = "\n".join(f"  CHUNK {ci}: {h['text'][:CHUNK_CHARS].replace(chr(10), ' ')}" for ci, h in enumerate(hits, 1))
        blocks.append(f"QUERY {qi}: {q}\n{chunks}")
    ctx.live("evaluator", "Judging retrieval relevance", f"{sum(len(state['evidence'][q]) for q in queries)} chunks "
                                                          f"across {len(queries)} queries")
    data, model = await llm.chat_json("judge", CONTEXT_PROMPT, {"blocks": "\n\n".join(blocks)}, temperature=0,
                                      max_tokens=1500, agent="evaluator", purpose="Grading retrieved chunks 0/1/2")
    graded = {int(_num(g.get("query"))): g.get("scores") or [] for g in data.get("grades", []) if isinstance(g, dict)}

    per_query, precisions, ndcgs, rrs = [], [], [], []
    for qi, q in enumerate(queries, 1):
        k = len(state["evidence"][q])
        scores = [max(0, min(2, int(_num(s)))) for s in graded.get(qi, [])][:k]
        if not scores:
            continue
        precision = sum(1 for s in scores if s >= 1) / len(scores)
        dcg = sum((2 ** s - 1) / math.log2(i + 2) for i, s in enumerate(scores))
        ideal = sum((2 ** s - 1) / math.log2(i + 2) for i, s in enumerate(sorted(scores, reverse=True)))
        ndcg = dcg / ideal if ideal else 0.0
        rr = next((1 / (i + 1) for i, s in enumerate(scores) if s == 2), 0.0)
        precisions.append(precision)
        ndcgs.append(ndcg)
        rrs.append(rr)
        per_query.append({"query": q, "scores": scores, "precision": _r(precision), "ndcg": _r(ndcg), "rr": _r(rr)})
    mean = lambda xs: _r(sum(xs) / len(xs)) if xs else None  # noqa: E731
    return {"context_precision": mean(precisions), "ndcg": mean(ndcgs), "mrr": mean(rrs),
            "per_query": per_query, "judge_model": model}


# ---------------------------------------------------------------- RAG: faithfulness
def _report_body(md: str) -> str:
    return re.split(r"\n## Sources\n", md)[0]


def _cited_claims(md: str) -> list[dict]:
    claims = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z*\[\-])|\n+", _report_body(md)):
        ids = sorted({int(n) for n in re.findall(r"\[(\d+)\]", sent)})
        text = re.sub(r"\[\d+\]|\*\*|__", "", sent).strip(" -*#>:")
        if ids and len(text.split()) >= 6:
            claims.append({"text": text[:400], "cited": ids})
    if len(claims) > MAX_CLAIMS:  # spread the sample across the whole report
        step = len(claims) / MAX_CLAIMS
        claims = [claims[int(i * step)] for i in range(MAX_CLAIMS)]
    return claims


def _support_excerpts(claim: str, source_id: int, by_source: dict[int, list[str]], n: int = 2) -> list[str]:
    toks = set(rag.tokenize(claim))
    chunks = by_source.get(source_id, [])
    ranked = sorted(chunks, key=lambda c: len(toks & set(rag.tokenize(c))), reverse=True)
    return [c[:600] for c in ranked[:n]]


async def faithfulness(ctx, state: dict, md: str) -> dict:
    claims = _cited_claims(md)
    if not claims:
        return {"faithfulness": None, "hallucination_rate": None, "claims": []}
    by_source: dict[int, list[str]] = defaultdict(list)
    for row in db.get_chunks(ctx.run_id):
        by_source[row["source_id"]].append(row["text"])
    blocks = []
    for i, c in enumerate(claims, 1):
        ev = []
        for sid in c["cited"][:2]:
            ev += [f"  [source {sid}] {e.replace(chr(10), ' ')}" for e in _support_excerpts(c["text"], sid, by_source)]
        blocks.append(f"CLAIM {i}: {c['text']}\nEVIDENCE:\n" + ("\n".join(ev) or "  (cited source has no indexed text)"))
    ctx.live("evaluator", "Fact-checking cited claims", f"{len(claims)} claims sampled from the final report")
    data, model = await llm.chat_json("judge", FAITH_PROMPT, {"blocks": "\n\n".join(blocks)}, temperature=0,
                                      max_tokens=2000, agent="evaluator", purpose="Checking claims against cited sources")
    verdicts = {int(_num(v.get("claim"))): v for v in data.get("verdicts", []) if isinstance(v, dict)}
    weight = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0, "contradicted": 0.0}
    out, total, bad = [], 0.0, 0
    for i, c in enumerate(claims, 1):
        v = verdicts.get(i, {})
        verdict = str(v.get("verdict", "unsupported")).lower()
        verdict = verdict if verdict in weight else "unsupported"
        total += weight[verdict]
        bad += verdict in ("unsupported", "contradicted")
        out.append({**c, "verdict": verdict, "reason": v.get("reason", "")})
    return {"faithfulness": _r(total / len(claims)), "hallucination_rate": _r(bad / len(claims)),
            "claims": out, "judge_model": model}


# ---------------------------------------------------------------- RAG: answer relevance + coverage
async def answer_quality(ctx, state: dict, md: str) -> dict:
    subqs = [sq["question"] for sq in (state.get("plan") or {}).get("subquestions", [])]
    ctx.live("evaluator", "Rating answer relevance", f"against the question and {len(subqs)} planned sub-questions")
    data, model = await llm.chat_json(
        "judge", ANSWER_PROMPT,
        {"question": state["question"], "subqs": "\n".join(f"{i}. {q}" for i, q in enumerate(subqs, 1)) or "(none)",
         "report": _report_body(md)[:12000]},
        temperature=0, max_tokens=800, agent="evaluator", purpose="Rating answer relevance and coverage")
    status = {int(_num(c.get("subquestion"))): str(c.get("status", "missing")).lower()
              for c in data.get("coverage", []) if isinstance(c, dict)}
    cov = [{"subquestion": q, "status": status.get(i, "missing")} for i, q in enumerate(subqs, 1)]
    score = {"full": 1.0, "partial": 0.5}
    coverage = _r(sum(score.get(c["status"], 0) for c in cov) / len(cov)) if cov else None
    rel = max(1.0, min(10.0, _num(data.get("answer_relevance"), 5)))
    return {"answer_relevance": _r(rel / 10), "subquestion_coverage": coverage, "coverage": cov,
            "rationale": data.get("rationale", ""), "judge_model": model}


# ---------------------------------------------------------------- deterministic metrics
async def retrieval_stats(ctx, state: dict) -> dict:
    index = rag.get_index(ctx.run_id)
    queries = _primary_queries(state)
    agreements, dense_kind = [], "local"
    for q in queries:
        k = len(state["evidence"][q]) or 6
        r = await index.rankings(q)
        dense_kind = r["dense_kind"]
        a, b = set(r["bm25"][:k]), set(r["dense"][:k])
        if a or b:
            agreements.append(len(a & b) / len(a | b))
    per_source = Counter(index.source_ids)
    return {
        "chunks_indexed": len(index.texts),
        "avg_chunks_per_source": _r(len(index.texts) / len(per_source), 1) if per_source else 0,
        "dense_retriever": dense_kind,
        "ranker_agreement": _r(sum(agreements) / len(agreements)) if agreements else None,
    }


def citation_stats(state: dict, md: str) -> dict:
    body = _report_body(md)
    sources = {s["id"]: s for s in state["sources"]}
    cited = [int(n) for n in re.findall(r"\[(\d+)\]", body)]
    valid = [c for c in cited if c in sources]
    paras = [p for p in body.split("\n\n") if len(p) > 220 and not p.lstrip().startswith(("#", "!", "|"))]
    uncited = [p for p in paras if not re.search(r"\[(\d+|analysis)\]", p)]
    words = len(body.split())
    domains = {urlparse(sources[c]["url"]).netloc for c in set(valid) if sources[c]["kind"] == "web"}
    web = [s for s in state["sources"] if s["kind"] == "web"]
    return {
        "citations": len(cited),
        "citation_precision": _r(len(valid) / len(cited)) if cited else None,
        "citation_coverage": _r(1 - len(uncited) / len(paras)) if paras else None,
        "citations_per_100_words": _r(100 * len(cited) / words, 2) if words else 0,
        "source_utilization": _r(len(set(valid)) / len(sources)) if sources else None,
        "unique_domains_cited": len(domains),
        "full_text_rate": _r(sum(s["full_text"] for s in web) / len(web)) if web else None,
        "report_words": words,
    }


def _query_diversity(queries: list[str]) -> float | None:
    sets = [set(rag.tokenize(q)) for q in queries if q]
    pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:] if a | b]
    if not pairs:
        return None
    return _r(1 - sum(len(a & b) / len(a | b) for a, b in pairs) / len(pairs))


def agent_metrics(run_id: str, state: dict) -> dict:
    ledger = db.query("SELECT agent, model, input_tokens, output_tokens, latency_ms, fallback FROM token_ledger "
                      "WHERE run_id=? ORDER BY id", (run_id,))
    agents: dict[str, dict] = {}
    for row in ledger:
        a = agents.setdefault(row["agent"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0,
                                             "fallbacks": 0, "models": []})
        a["calls"] += 1
        a["input_tokens"] += row["input_tokens"]
        a["output_tokens"] += row["output_tokens"]
        a["latency_ms"] += row["latency_ms"] or 0
        a["fallbacks"] += row["fallback"]
        if row["model"] not in a["models"]:
            a["models"].append(row["model"])
    for a in agents.values():
        a["avg_latency_s"] = _r(a["latency_ms"] / a["calls"] / 1000, 2) if a["calls"] else 0
        a["fallback_rate"] = _r(a["fallbacks"] / a["calls"]) if a["calls"] else 0

    # wall time per stage from the node-start events
    events = db.get_events(run_id)
    stage_time: dict[str, float] = defaultdict(float)
    nodes = [(e["ts"], e["data"].get("node")) for e in events if e["type"] == "node"]
    end_ts = events[-1]["ts"] if events else 0
    for (ts, name), nxt in zip(nodes, nodes[1:] + [(end_ts, None)]):
        stage_time[name] += max(0.0, nxt[0] - ts)

    plan = state.get("plan") or {}
    queries = [q for sq in plan.get("subquestions", []) for q in sq.get("search_queries", [])]
    searches = sum(1 for e in events if e["type"] == "log" and e["message"].startswith("Searching:"))
    web = [s for s in state["sources"] if s["kind"] == "web"]
    crits = state.get("critiques", [])
    scores = [c["overall"] for c in crits]
    issues = [len(c.get("issues", [])) for c in crits]
    high = [sum(1 for i in c.get("issues", []) if str(i.get("severity", "")).lower() == "high") for c in crits]
    analysis = state.get("analysis")
    total_tokens = sum(r["input_tokens"] + r["output_tokens"] for r in ledger)
    return {
        "per_agent": agents,
        "stage_seconds": {k: _r(v, 1) for k, v in stage_time.items() if k},
        "wall_seconds": _r(end_ts - events[0]["ts"], 1) if events else 0,
        "total_tokens": total_tokens,
        "llm_calls": len(ledger),
        "fallback_rate": _r(sum(r["fallback"] for r in ledger) / len(ledger)) if ledger else 0,
        "planner": {"subquestions": len(plan.get("subquestions", [])), "queries": len(queries),
                    "query_diversity": _query_diversity(queries)},
        "researcher": {"searches": searches, "web_sources": len(web),
                       "sources_per_search": _r(len(web) / searches, 2) if searches else None},
        "critic_loop": {
            "scores": scores, "first_score": scores[0] if scores else None, "best_score": max(scores) if scores else None,
            "improvement": _r(max(scores) - scores[0], 2) if scores else None,
            "revisions": max(0, len(crits) - 1), "passed": any(c.get("verdict") == "pass" for c in crits),
            "issues_first": issues[0] if issues else None, "issues_last": issues[-1] if issues else None,
            "high_issues_first": high[0] if high else None, "high_issues_last": high[-1] if high else None,
        },
        "analyst": ({"success": analysis.get("success"), "attempts": analysis.get("attempts")} if analysis else None),
    }


def overall_score(rag_m: dict, best_critic: float | None) -> int:
    """0-100 composite. Missing parts are dropped and the remaining weights renormalised."""
    parts = [(0.35, rag_m.get("faithfulness")), (0.20, rag_m.get("context_precision")),
             (0.20, rag_m.get("answer_relevance")), (0.15, best_critic / 10 if best_critic else None),
             (0.10, rag_m.get("citation_precision"))]
    parts = [(w, v) for w, v in parts if v is not None]
    if not parts:
        return 0
    return round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts))


async def evaluate(ctx, state: dict) -> dict:
    md = state.get("final_report") or (state["drafts"][-1]["markdown"] if state.get("drafts") else "")
    if not md:
        raise ValueError("No report to evaluate")
    ctx.live("evaluator", "Computing retrieval statistics", "BM25 vs dense ranker agreement, chunk coverage")
    retrieval = await retrieval_stats(ctx, state)
    cites = citation_stats(state, md)
    ctx_rel = await context_relevance(ctx, state)
    faith = await faithfulness(ctx, state, md)
    answer = await answer_quality(ctx, state, md)
    agents = agent_metrics(ctx.run_id, state)
    rag_m = {
        "context_precision": ctx_rel["context_precision"], "ndcg": ctx_rel["ndcg"], "mrr": ctx_rel["mrr"],
        "faithfulness": faith["faithfulness"], "hallucination_rate": faith["hallucination_rate"],
        "answer_relevance": answer["answer_relevance"], "subquestion_coverage": answer["subquestion_coverage"],
        **{k: cites[k] for k in ("citation_precision", "citation_coverage", "source_utilization")},
        "ranker_agreement": retrieval["ranker_agreement"],
    }
    return {
        "overall": overall_score(rag_m, agents["critic_loop"]["best_score"]),
        "rag": rag_m,
        "retrieval": retrieval,
        "citations": cites,
        "agents": agents,
        "details": {"per_query": ctx_rel["per_query"], "claims": faith["claims"], "coverage": answer["coverage"],
                    "rationale": answer["rationale"]},
        "judge_models": sorted({m for m in (ctx_rel.get("judge_model"), faith.get("judge_model"),
                                            answer.get("judge_model")) if m}),
    }


def summary_row(run: dict, e: dict) -> dict:
    """Flat row for the evaluations dashboard."""
    return {
        "id": run["id"], "question": run["question"], "created_at": run["created_at"],
        "username": run.get("username"), "overall": e.get("overall"), **e.get("rag", {}),
        "best_critic": e.get("agents", {}).get("critic_loop", {}).get("best_score"),
        "tokens": e.get("agents", {}).get("total_tokens"), "wall_seconds": e.get("agents", {}).get("wall_seconds"),
        "fallback_rate": e.get("agents", {}).get("fallback_rate"),
    }
