# ResearchX code guide

A file-by-file, function-by-function map of the code. Read "How a request flows" first, then jump to the
file you're changing. HTTP endpoints are listed in detail in [API.md](API.md).

```
ResearchX/
├── start.sh                 one-command launcher (deps, backend, health wait, Vite)
├── .github/workflows/ci-cd.yml  CI (tests, build) + CD (GHCR → EC2 → Vercel), see DEPLOYMENT.md
├── deploy/
│   ├── docker-compose.yml   production stack on EC2: api + cloudflared
│   ├── ec2-setup.sh         one-time server bootstrap (Docker, swap, /opt/researchx)
│   └── render-vercel-json.sh writes frontend/vercel.json (SPA + /api rewrite to the backend)
├── backend/
│   ├── Dockerfile           production image (python 3.12-slim, non-root, 1 worker, healthcheck)
│   ├── tests/               pytest API tests (conftest.py sets a temp DATA_DIR + test admin)
│   ├── requirements.txt     Python dependencies (requirements-dev.txt adds pytest)
│   ├── .env.example         configuration template (copy to .env)
│   ├── data/                (created at runtime, git-ignored) SQLite DBs, uploads, charts, backend.log
│   └── app/
│       ├── config.py        settings from .env
│       ├── db.py            SQLite schema + data access
│       ├── auth.py          accounts, sessions, roles, token pool
│       ├── meter.py         per-run token metering + live activity state
│       ├── llm.py           model chains, streaming, JSON parsing, embeddings
│       ├── search.py        web search + page/PDF extraction
│       ├── rag.py           chunking, BM25, dense vectors, hybrid retriever
│       ├── analysis.py      LLM-written pandas analysis in a subprocess
│       ├── agents.py        the agents (LangGraph nodes) + follow-up Q&A
│       ├── evals.py         RAG + agent evaluation metrics
│       ├── orchestrator.py  LangGraph graph, run tasks, checkpoints
│       └── main.py          FastAPI app: every HTTP endpoint
├── frontend/
│   ├── index.html           page shell, fonts, theme bootstrap
│   ├── vite.config.js       dev server + /api proxy to the backend
│   └── src/                 React UI (see "Frontend")
└── docs/                    API.md, this file, screenshots
```

## How a request flows

1. **Sign in.** `Landing.jsx` → `POST /api/auth/login|signup|guest` → `auth.py` creates/validates the user and
   `main._set_session` sets the `rx_session` cookie. Every later request resolves the user via `auth.current_user`.
2. **Start research.** `Composer.jsx` → `POST /api/runs` → `main.create_run` checks role limits and balance,
   saves uploads, `orchestrator.new_state` builds the initial state, `db.create_run` stores it, and
   `orchestrator.start` launches an asyncio task.
3. **The graph runs.** `orchestrator._run` sets `meter.scope` (who pays) and invokes the LangGraph graph.
   Each node in `agents.NODES` does one job and returns the name of the next node:
   `plan → research → [analyze] → write → critique → (gap_research → revise → critique)* → finalize → evaluate`.
4. **Every model call** goes through `llm.chat`: budget check → stream tokens (updating the live panel) →
   record usage in the token ledger → log an `llm` event.
5. **The browser watches.** `RunView.jsx` opens `GET /api/runs/{id}/events` (SSE). `main.stream_events`
   sends stored events plus `live` snapshots from `meter`, which the terminal panel renders.
6. **Out of tokens?** `meter.check_budget` raises `BudgetExceeded`; `_run` marks the run `paused`.
   After a top-up, `POST /resume` continues from the LangGraph checkpoint at the same node.

---

## Backend (`backend/app/`)

### `config.py`
Loads `backend/.env`, creates `data/`, `data/uploads/`, `data/outputs/`, and exposes settings as module constants:
API keys, `ROLE_MODELS` (fallback chain per role: planner, fast, writer, critic, analyst, judge), `EMBEDDING_MODEL`,
`MAX_REVISIONS`, `PASS_SCORE`, `SEARCH_RESULTS_PER_QUERY`, `MAX_SOURCES`, and auth settings
(`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SESSION_DAYS`, `GUEST_HOURS`, `GUESTS_PER_IP_PER_DAY`, `COOKIE_SECURE`), and
deployment settings: `DATA_DIR` (override the data folder), `TRUST_PROXY` (take the client IP from
`X-Forwarded-For`), `CORS_ORIGINS` (who may open the live stream cross-origin), `SECRET_KEY` (signs stream tokens).

| Function | What it does |
|---|---|
| `env(name, default)` | Read an environment variable, stripped of whitespace. |

### `db.py`
One SQLite connection (WAL mode, guarded by a lock) to `data/researchx.db`. Creates tables on import and
migrates older databases (adds `runs.user_id`, `runs.tokens_used`).

Tables: `runs` (question, status, full JSON state, owner, tokens), `events` (the run log), `chunks` (RAG text +
embedding blob), `users`, `sessions` (hashed tokens), `settings` (key → JSON), `token_ledger` (one row per LLM call).

| Function | What it does |
|---|---|
| `_exec(sql, params)` / `_query(sql, params)` | Locked write (with commit) / locked read. |
| `create_run(question, state, user_id)` | Insert a run with status `queued`; returns the 12-char id. |
| `save_run(run_id, status, state)` | Update status + JSON state. |
| `get_run(run_id)` | Run row with `state` decoded, or `None`. |
| `list_runs(user_id, limit)` | A user's runs, or all runs with owner names when `user_id` is `None` (admin). |
| `run_owner(run_id)` | The owning user id. |
| `add_run_tokens(run_id, n)` | Add to the run's token total. |
| `delete_run(run_id)` | Delete run, events and chunks (ledger rows are kept so spent tokens stay counted). |
| `runs_with_status(*statuses)` | Ids of runs in the given statuses. |
| `add_event(run_id, type, agent, message, data)` | Append to the event log; returns the event dict. |
| `get_events(run_id, after_id)` | Events newer than `after_id`. |
| `add_chunks(run_id, items)` / `get_chunks(run_id)` | Store / load RAG chunks for a run. |
| `execute(sql, params)` / `query(sql, params)` | Public helpers used by auth, admin and evals (`query` returns dicts). |
| `get_setting(key, default)` / `set_setting(key, value)` | JSON key/value settings (admin policy). |

### `auth.py`
Accounts, sessions and the token pool. Roles: `admin`, `user`, `guest`.
Pool math: `available = token_pool − consumed (ledger total) − committed (unspent part of active allotments)`.

| Function / class | What it does |
|---|---|
| `DEFAULT_SETTINGS` | Defaults for pool size, sign-up/guest allotments, guest switches and limits. |
| `settings()` | Current admin policy (stored values over defaults). |
| `update_settings(patch)` | Validate and save policy; refuses to shrink the pool below consumed + committed. |
| `hash_password(pw)` / `verify_password(pw, stored)` | PBKDF2-SHA256 (240k rounds, random salt), constant-time compare. |
| `User` | Dataclass for a user row; `is_admin`, `is_guest` properties. |
| `_row_to_user(row)` / `get_user(id)` | Load a `User`. |
| `create_user(username, password, role, token_limit, expires_at, ip)` | Validate and insert; 409 if the name is taken. |
| `remaining(user)` | Tokens the user may still spend: their allotment left, capped by the pool (admin: pool `available`). |
| `public_user(user)` | JSON-safe user with `tokens_left`. |
| `ensure_admin()` | On startup: create the admin from `.env` (or generate a password and log it), promote an existing user with that name if needed, and assign pre-auth runs to the admin. Safe if two servers start at once. |
| `pool_status()` | `{pool, consumed, committed, available}`. |
| `grant(user, new_limit)` | Set an allotment if the pool can cover the increase, else 400. |
| `_hash_token(token)` | SHA-256 of a session token (only hashes are stored). |
| `new_session(user)` / `end_session(token)` | Create (14 days, or the guest pass length) / delete a session. |
| `user_from_token(token)` | Resolve a cookie to an active, non-expired user and update `last_seen`. |
| `purge_expired()` | Delete expired sessions and disable expired guests. |
| `guests_today_from(ip)` | Guest passes created from an IP in the last 24h (abuse limit). |
| `current_user(request)` | FastAPI dependency: the signed-in user, or 401. |
| `admin_user(user)` | FastAPI dependency: the user if admin, or 403. |
| `client_ip(request)` | Caller's IP; behind trusted proxies (`TRUST_PROXY`) the first `X-Forwarded-For` / `CF-Connecting-IP`. |
| `_secret()` | HMAC key: `SECRET_KEY`, or a random key generated once and stored in `settings`. |
| `stream_token(run_id, user_id)` | `run:user:expiry:hmac` token (6h) that authorises one run's event stream without cookies. |
| `user_from_stream_token(token, run_id)` | Verify signature, run id and expiry; return the user or `None`. |

### `meter.py`
Who pays for a model call, and what the run is doing right now.

| Function / class | What it does |
|---|---|
| `BudgetExceeded` | Raised when the run owner has no tokens left; the orchestrator pauses the run. |
| `Scope` / `scope` | `ContextVar` holding `(run_id, user_id)`, set once per run task so every LLM call is charged correctly. |
| `live_get(run_id)` | Current live snapshot (or `None`). |
| `live_set(run_id, **fields)` | Update the snapshot (stage, agent, action, detail, model, counters); a new action resets the timer and stream. Bumps the version `v` so SSE resends it. |
| `live_stream(text)` | Store the tail of the model's streamed output for the preview. |
| `live_clear(run_id)` | Drop the snapshot when the run stops. |
| `check_budget()` | Raise `BudgetExceeded` if the scoped user has 0 tokens left. |
| `record(agent, role, model, in, out, latency_ms, fallback, estimated)` | Write a ledger row, charge the user and run, and push the new balance to the live panel. |

### `llm.py`
LangChain model layer over Groq, Gemini, OpenRouter and Ollama.

| Function / class | What it does |
|---|---|
| `_RATES` / `_limiters` | Per-provider request-rate limiters, sized for free tiers. |
| `THINKING_BUDGET` | Cap on hidden reasoning tokens, added on top of `max_tokens` so drafts aren't truncated. |
| `LLMError` / `LLMResult` | Error type / result (`text`, `model`, `tokens`). |
| `provider_available(provider)` | Is there a key/URL for this provider? |
| `chain_for(role)` | The role's `(provider, model)` list, skipping providers without keys. |
| `available_models()` | Chains for every role (shown in `/api/health`). |
| `_make_model(provider, model, temperature, max_tokens, json_mode)` | Build one LangChain chat model with retries, rate limiting and JSON mode. |
| `model_for(role, ...)` | Cached `primary.with_fallbacks([...])` for a role. |
| `_prompt_chars(prompt, variables)` | Prompt size, used to estimate tokens. |
| `chat(role, prompt, variables, *, agent, purpose, ...)` | The single entry point for model calls: budget check → stream (live preview) → normalise `【n】` citations → usage from provider metadata (else ~4 chars/token) → fallback detection → ledger + `llm` event. |
| `parse_json(text)` | Pull JSON out of a reply (strips code fences, finds the outermost object/array). |
| `chat_json(role, prompt, variables, **kw)` | `chat` in JSON mode with one repair round-trip on invalid JSON. Returns `(data, model)`. |
| `embedder()` / `embed(texts, task)` | Gemini embeddings (768-d) or `None` so callers fall back to local vectors. |

### `search.py`
| Function | What it does |
|---|---|
| `_ddgs(query, n)` | Free metasearch via `ddgs`, trying DuckDuckGo → Bing → Brave → Mojeek → Yahoo. |
| `_wikipedia(query, n)` | Wikipedia search API (last resort). |
| `_tavily(query, n)` | Tavily search when `TAVILY_API_KEY` is set. |
| `web_search(query, n)` | Tavily → ddgs → Wikipedia; drops social/video sites. |
| `_extract_html(html, url)` | Main text via trafilatura, else BeautifulSoup cleanup. |
| `extract_pdf(data)` | Text of up to 60 PDF pages. |
| `fetch_page(url, client)` | Download and extract one page or PDF ("" on failure). |
| `fetch_many(urls, concurrency)` | Fetch pages in parallel (6 at a time). |

### `rag.py`
Hybrid retrieval over one run's corpus.

| Function / class | What it does |
|---|---|
| `tokenize(text)` | Lowercase word tokens without stop words (used by BM25, audits and evals). |
| `chunk_text(text)` | `RecursiveCharacterTextSplitter` (900 chars, 150 overlap), drops tiny chunks. |
| `hashed_vector(text)` | Local 2048-d hashed unigram+bigram vector (fallback when there are no embeddings). |
| `BM25` (`scores(query)`) | Okapi BM25 scoring over tokenized chunks. |
| `RunIndex` | In-memory index of a run's chunks: BM25, local vectors, Gemini vectors. |
| `RunIndex.rankings(query)` | The separate BM25 and dense rankings (also used by the ranker-agreement metric). |
| `RunIndex.search(query, k, per_source)` | Reciprocal Rank Fusion of both rankings, at most `per_source` chunks per source. |
| `index_document(run_id, source_id, text)` | Chunk, embed, store; invalidates the cached index. |
| `get_index(run_id)` | Cached `RunIndex`. |
| `HybridRetriever` | LangChain `BaseRetriever` wrapper (async only; `ainvoke(query)` → `Document`s). |

### `analysis.py`
| Function | What it does |
|---|---|
| `load_frame(path)` | Read CSV/TSV/XLSX/JSON into pandas. |
| `profile(path)` | Shape, dtypes, head and describe() — what the analyst model sees. |
| `_execute(code, out_dir, timeout)` | Reject obviously dangerous code, run it in a subprocess (`python -I`, 60s timeout), return `(ok, output)`. **Not a sandbox.** |
| `run_analysis(question, goals, datasets, out_dir, emit)` | Ask the analyst model for a script, run it, and on failure feed the traceback back for up to 3 attempts. Returns code, output, charts, explanation. |

### `agents.py`
The LangGraph nodes. Each takes `(ctx, state)` and returns the next node's name.

| Function / class | What it does |
|---|---|
| `Ctx` | `run_id`, `emit(agent, message, data, type)` for the event log, and `live(agent, action, detail)` for the live panel. |
| `DEPTH` / `EVIDENCE_BUDGET` | Sub-questions, queries and chunks per depth; max evidence characters sent to the writer. |
| `_track(state, role, model)` | Remember which model served each role. |
| `plan_node` | Planner: sub-questions, search queries, report outline, analysis goals → `research`. |
| `_ingest_urls(ctx, state, results, agent)` | Fetch new result pages, add them as numbered sources, index them. |
| `_run_queries(ctx, queries, agent)` | Run searches one by one (polite to free endpoints). |
| `_gather_evidence(ctx, state, questions, key_prefix)` | Hybrid retrieval for each question into `state["evidence"]`. |
| `research_node` | Index uploaded docs, search + ingest, retrieve evidence → `analyze` (if datasets) or `write`. |
| `analyze_node` | Run dataset analysis; failure doesn't stop the run → `write`. |
| `format_evidence(state, budget)` | Evidence grouped by source, round-robin across questions, within a character budget. |
| `format_analysis(state)` | Analysis results as prompt text. |
| `_strip_refs(md)` | Remove any references list the model added (the system appends its own). |
| `write_node` | Writer: cited Markdown draft → `critique`. |
| `citation_audit(markdown, state)` | Deterministic audit: invalid citation ids, uncited paragraphs, low-overlap ("weak") citations. |
| `critique_node` | Critic scores 5 criteria + audit penalty; pass → `finalize`, else `gap_research` (or `finalize` when out of revisions). |
| `gap_research_node` | Search the critic's follow-up queries, retrieve for gaps → `revise`. |
| `revise_node` | Reviser rewrites the draft and lists its changes → `critique`. |
| `finalize_node` | Pick the best-scoring draft, link charts, append the cited sources list → `evaluate`. |
| `evaluate_node` | Run `evals.evaluate`; never fails the run (no tokens → note to evaluate later) → `done`. |
| `answer_followup(ctx, state, question)` | "Ask the vault": retrieve from the run's sources and answer with citations. |
| `NODES` | Node name → function map used to build the graph. |

### `evals.py`
| Function | What it does |
|---|---|
| `context_relevance(ctx, state)` | Judge grades each retrieved chunk 0/1/2 → context precision, nDCG, MRR, per-query table. |
| `_cited_claims(md)` | Up to 12 cited sentences spread across the report. |
| `_support_excerpts(claim, source_id, by_source)` | Best-matching chunks of a cited source for a claim. |
| `faithfulness(ctx, state, md)` | Judge labels claims supported/partial/unsupported/contradicted → faithfulness, hallucination rate. |
| `answer_quality(ctx, state, md)` | Judge rates answer relevance and coverage of each sub-question. |
| `retrieval_stats(ctx, state)` | Chunk counts, dense retriever type, BM25-vs-dense top-k overlap. |
| `citation_stats(state, md)` | Citation precision/coverage/density, source utilization, domains, full-text rate. |
| `_query_diversity(queries)` | 1 − mean pairwise Jaccard similarity of the planner's queries. |
| `agent_metrics(run_id, state)` | Per-agent calls/tokens/latency/fallbacks (ledger), time per stage (events), planner/researcher/critic-loop/analyst stats. |
| `overall_score(rag, best_critic)` | 0–100 weighted composite (missing parts renormalised). |
| `evaluate(ctx, state)` | Runs all of the above and returns the evaluation stored in `state["evaluation"]`. |
| `summary_row(run, e)` | Flat row for the evaluations dashboard. |
| `_num`, `_r`, `_primary_queries`, `_report_body` | Small helpers: safe float, rounding, non-gap queries, report without the Sources list. |

### `orchestrator.py`
| Function / class | What it does |
|---|---|
| `ResearchState` | TypedDict of the run state (question, plan, sources, evidence, drafts, critiques, evaluation, qa, …). |
| `new_state(question, depth, max_revisions, documents, datasets)` | Initial state for a run. |
| `STAGES` | Human label + agent for each node (shown in the live panel). |
| `_emitter(run_id)` | `emit` function: writes events and updates the live detail line. |
| `_node(name, fn)` | Wrap an agent as a LangGraph node: save state, set live stage, log `node`, run, save. |
| `_route(state)` | Conditional edge: go to `state["next_node"]` (or END on `done`). |
| `build_graph(checkpointer)` | Compile the StateGraph with every node routable from every other. |
| `init()` / `shutdown()` | Open the SQLite checkpointer and graph / cancel tasks and close it. |
| `_scope(run_id)` | Set `meter.scope` for the current task. |
| `_run(run_id)` | Run or resume the graph; sets `completed`, `paused` (budget), `cancelled` or `failed`. |
| `_evaluate(run_id)` / `start_evaluation(run_id)` | Evaluate an already-finished run in a background task. |
| `start(run_id)` / `cancel(run_id)` / `is_active(run_id)` | Manage run tasks. **Must be called from async code** (they use `asyncio.create_task`). |
| `forget(run_id)` | Cancel and delete the run's checkpoints. |
| `mark_interrupted()` | On startup, mark runs left `running` as `interrupted` (resumable). |

### `main.py`
The FastAPI app. Endpoints are documented in [API.md](API.md); helpers:

| Function | What it does |
|---|---|
| `lifespan` | Startup: ensure admin, purge expired guests, mark interrupted runs, open the graph. |
| `unhandled_error` | Turn unexpected exceptions into JSON `{"detail": ...}` (traceback goes to the log). |
| `_set_session(response, user)` | Set the httpOnly session cookie. |
| `_estimates()` | Average tokens per depth from finished runs (defaults until there is data). |
| `_owned_run(run_id, user)` | Load a run the user owns (admins may open any), else 404. |
| `get_stream_token` | `GET /api/runs/{id}/stream-token`: signed token for opening the stream on the API host. |
| `stream_events` | SSE loop: new events, `live` snapshots when they change, keepalives, `end` when idle. Accepts the cookie or `?st=<token>`, and resumes from `Last-Event-ID` after a reconnect. |
| `eval_dashboard` | Aggregates per-run evaluations, averages and per-agent totals. |
| Admin endpoints | Pool overview, users CRUD with allotments, policy settings, guest purge. |

`CORSMiddleware` allows cross-origin `GET` without credentials (for the token-authenticated stream); cookies never
work cross-site. `/api/health` includes `version` (`APP_VERSION`, set to the commit SHA by the image build), which the
deploy job checks. The built frontend (`frontend/dist`) is served at `/` when it exists, so one process can serve everything.

---

## Frontend (`frontend/src/`)

### `main.jsx`
Mounts `<App/>` and loads `styles.css`.

### `api.js`
| Export | What it does |
|---|---|
| `STREAM_BASE` | `VITE_API_STREAM_BASE` (build-time). When set, the event stream goes straight to the API host with a stream token. |
| `req(path, opts)` | `fetch` wrapper: JSON bodies, readable errors (network failure → "Backend not reachable"), dispatches `rx:unauthorized` on 401. |
| `api.*` | One method per endpoint: auth (`login`, `signup`, `guest`, `me`, `upgrade`, …), runs (`create`, `resume`, `evaluate`, `ask`, `events` → `EventSource`), `evals`, admin (`overview`, `users`, `updateUser`, `saveSettings`, …). |

### `App.jsx`
| Function | What it does |
|---|---|
| `parseRoute()` | Hash routes: `#/`, `#/run/<id>`, `#/evals`, `#/admin`, `#/account` (old `?run=` links still work). |
| `App` | Loads the user; shows `Landing` when signed out. While the backend is unreachable it shows a banner and retries every 3s. Signed in: sidebar (nav, archive of runs — all users for admin, guest banner, wallet, user bar) and the routed page. Receives live token updates from runs (`onTokens`). |

### `Landing.jsx`
| Function | What it does |
|---|---|
| `Landing` | Public page: hero, two entrances (Reading Room / Keeper's Office), agents, metrics, offline banner. |
| `AuthCard` | Sign in / Create account tabs and "Continue as guest" (always visible; disabled with a reason if the admin closed them). The Keeper's entrance only accepts admin accounts. |
| `ThemeToggle` | Switch vault (dark) / parchment (light) theme; remembered in `localStorage`. |

### `Composer.jsx`
`Composer` — new research form: question, file drop (disabled for guests unless allowed), depth (guest max depth
enforced), revisions, token estimate vs balance, example questions.

### `RunView.jsx`
| Function | What it does |
|---|---|
| `RunView` | Loads a run, subscribes to its SSE stream (events + `live`; `attach` wires the handlers; the browser reconnects automatically), header actions (stop, resume, download, delete), tabs. |
| `Pipeline` | Stage pills showing done / current / paused / skipped. |
| `useNow`, `dur` | Ticking clock and duration formatting for the live panel. |
| `Terminal` | The live terminal: current stage/agent/action/detail with timers, streamed model output, tokens this run / left, and the scrolling log (toggle model-call lines). |
| `Evaluation` | Run grade gauge, RAG metric cards, per-agent table, time per stage, fact-check list, retrieval relevance per query, coverage; button to (re-)run. |
| `Ask` | Follow-up questions over the run's sources and past answers. |
| `ScoreStrip`, `ScoreChart` | Critic score per draft. |
| `Markdown` | Report renderer; turns `[n]` into citation links to the source. |
| `Review` | Each critique: scores, citation audit, issues, reviser changes, the draft itself. |
| `Sources`, `Analysis`, `Plan` | Source list, dataset analysis output/charts/code, planner output and models used. |
| `download(run)` | Save the final report as Markdown. |

### `Evals.jsx`
| Function | What it does |
|---|---|
| `Evals` | Averages across evaluated runs, run table (click to open), per-agent totals, "evaluate" for unevaluated runs; admin can switch Mine / All users. |
| `Trend` | SVG line chart of overall / faithfulness / context precision over the last 30 runs. |

### `Admin.jsx`
| Function | What it does |
|---|---|
| `Admin` | Keeper's Office page: pool, counts, policy, usage charts, accounts table, new-account dialog. |
| `PoolStat`, `PoolEditor` | Pool numbers; edit the pool size. |
| `Policy` | Guest mode (tokens, max depth, uploads) and sign-ups (open/closed, new-account tokens). |
| `UserRow` | One account: usage meter, set allotment / +25k, extend guest pass, reset password, enable/disable, delete. |
| `CreateUser` | Modal to create a researcher or admin. |
| `DayBars`, `Bars` | Tokens per day; tokens by agent / model. |

### `Account.jsx`
| Function | What it does |
|---|---|
| `Account` | Wallet and where your tokens went (by agent). |
| `Upgrade` | Guest → account (keeps runs). |
| `Password` | Change password. |

### `ui.jsx`
Shared helpers and components: `fmt`, `kfmt`, `pct`, `ago`, `catalogNo` (formatting); `AGENT_COLORS`;
`VaultMark` (logo); `Icon`; `Wallet` (token balance card); `Gauge` (0–100 ring); `level` + `Metric` (metric card
with good/ok/bad colouring); `useToast`; `Empty` (placeholder with spinner).

### `styles.css`
Design tokens for the vault (dark) and parchment (light) themes on `:root`, then layout and component styles
(landing, shell, composer, run view, terminal, evaluation, admin) and responsive rules down to phone width.

---

## Common changes

- **Swap models:** edit `MODEL_<ROLE>` in `backend/.env` (comma-separated fallback chain). No code changes.
- **Add an agent/step:** write `async def my_node(ctx, state) -> str` in `agents.py`, add it to `NODES`, add a label to
  `orchestrator.STAGES`, and make an existing node return `"my_node"`. Add it to `STEPS` in `RunView.jsx` for the pipeline view.
- **Add a metric:** compute it in `evals.evaluate` (put it in `rag`), add it to `RAG_METRICS` in `RunView.jsx` and,
  for the dashboard, to `METRICS` in `main.py` and `TILES` in `Evals.jsx`.
- **Add an endpoint:** in `main.py`; use `Depends(current_user)` / `Depends(admin_user)`. If it starts a background
  task, declare it `async def` (sync endpoints run in a thread with no event loop).
