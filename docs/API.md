# ResearchX HTTP API

All endpoints are under `/api`. Authentication is a session cookie (`rx_session`, httpOnly) set by the
login, signup and guest endpoints. Errors are JSON `{"detail": "..."}` with a meaningful status code
(401 not signed in, 402 out of tokens, 403 not allowed, 404 not found / not yours, 409 username taken, 429 rate limited).

## Auth

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/auth/config` | – | Public: guest/sign-up switches and default allotments |
| POST | `/auth/login` | `{username, password}` | 5 failed attempts per IP+username → 10 min lockout |
| POST | `/auth/signup` | `{username, password}` | Only when sign-ups are open; gets the default allotment |
| POST | `/auth/guest` | – | Guest pass (`GUEST_HOURS`), limited per IP per day |
| POST | `/auth/upgrade` | `{username, password}` | Guest → account; runs and unused tokens carry over |
| POST | `/auth/password` | `{current, new}` | Change own password |
| POST | `/auth/logout` | – | |
| GET | `/auth/me` | – | User with `tokens_used`, `token_limit`, `tokens_left` |
| GET | `/me/usage` | – | Own token use by day (14d) and by agent |

## Runs

| Method | Path | Notes |
|---|---|---|
| GET | `/estimates` | Average tokens per depth, depths and uploads allowed for this user |
| POST | `/runs` | multipart: `question`, `depth` (quick/standard/deep), `max_revisions` (0–4), `files[]` |
| GET | `/runs?all=true` | Own runs (`all` = every user's, admin only) |
| GET | `/runs/{id}` | Full run state: plan, sources, drafts, critiques, final report, evaluation, Q&A |
| POST | `/runs/{id}/resume` | Resume a failed/cancelled/interrupted/paused run from its last step |
| POST | `/runs/{id}/cancel` | Stop a running job (resumable) |
| DELETE | `/runs/{id}` | Delete run, events and chunks (token ledger rows are kept) |
| POST | `/runs/{id}/evaluate` | (Re-)evaluate a finished run |
| POST | `/runs/{id}/ask` | `{question}` → cited answer from the run's indexed sources |
| GET | `/runs/{id}/stream-token` | `{token}` to open the event stream on another origin (valid 6h, one run) |
| GET | `/runs/{id}/events?after=N[&st=token]` | Server-sent events, see below. Auth: session cookie, or `st` |
| GET | `/runs/{id}/files/{name}` | Charts produced by the analyst |

### Event stream

`GET /runs/{id}/events` is `text/event-stream` with three kinds of messages:

- default `message`: a persisted log event `{id, ts, type, agent, message, data}`. Types include
  `node` (stage started), `log`, `source`, `plan`, `llm` (model, tokens in/out, latency, fallback), `draft`,
  `critique`, `final`, `evaluation`, `budget` (paused for tokens), `error`, `done`.
- `event: live`: the current activity snapshot `{node, stage, agent, action, detail, model, stream,
  stream_chars, waiting, run_tokens, tokens_left, stage_started, action_started}`; sent whenever it changes.
- `event: end`: the run is no longer active.

Each event carries an SSE `id`, so a reconnecting `EventSource` sends `Last-Event-ID` and the stream resumes after it.
In production the frontend opens the stream directly on the API host:
`GET https://api.<domain>/api/runs/{id}/events?st=<token>` (CORS-enabled, no cookies).

## Evaluations

| Method | Path | Notes |
|---|---|---|
| GET | `/evals?all=true` | Per-run metric rows, averages, per-agent totals, finished-but-unevaluated runs |

Metric definitions are in the README and at the top of `backend/app/evals.py`.

## Admin (role `admin`)

| Method | Path | Body / notes |
|---|---|---|
| GET | `/admin/overview` | Pool status, account and run counts, usage by day/model/agent |
| GET | `/admin/users` | All accounts with usage and run counts |
| POST | `/admin/users` | `{username, password, role: user|admin, token_limit}` |
| PATCH | `/admin/users/{id}` | Any of `token_limit`, `add_tokens`, `disabled`, `role`, `extend_hours` (guests), `password` |
| DELETE | `/admin/users/{id}` | Deletes the account and its runs |
| GET / PUT | `/admin/settings` | `token_pool`, `default_user_tokens`, `guest_tokens`, `guest_enabled`, `signup_enabled`, `guest_max_depth`, `guest_uploads` |
| POST | `/admin/guests/purge` | Close expired guest passes |

Pool accounting: `available = token_pool − consumed − committed`, where `consumed` is the ledger total and
`committed` is the unspent part of active allotments. Grants that exceed `available` are rejected.
