#!/usr/bin/env bash
# One-command launcher: sets up deps on first run, starts the backend, waits until it answers,
# then starts the Vite dev server (which proxies /api to the backend).
#   BACKEND_PORT=8000 FRONTEND_PORT=5173 ./start.sh
set -e
cd "$(dirname "$0")"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export BACKEND_PORT FRONTEND_PORT

port_busy() { lsof -iTCP:"$1" -sTCP:LISTEN -n -P >/dev/null 2>&1; }

if [ ! -d backend/.venv ]; then
  echo "→ Creating Python virtualenv and installing backend dependencies (first run only)…"
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
[ -f backend/.env ] || { cp backend/.env.example backend/.env; echo "→ Created backend/.env — add your free API keys and ADMIN_PASSWORD there."; }
[ -d frontend/node_modules ] || { echo "→ Installing frontend dependencies…"; (cd frontend && npm install --no-audit --no-fund); }

for p in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if port_busy "$p"; then
    echo "✗ Port $p is already in use by:"
    lsof -iTCP:"$p" -sTCP:LISTEN -n -P | tail -n +2
    echo "  Stop that process (kill <PID>) or pick other ports, e.g. BACKEND_PORT=8001 FRONTEND_PORT=5174 ./start.sh"
    exit 1
  fi
done

mkdir -p backend/data
LOG=backend/data/backend.log
echo "→ Starting backend on http://127.0.0.1:$BACKEND_PORT (log: $LOG)"
(cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT") >"$LOG" 2>&1 &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT INT TERM

# The first start compiles LangChain/LangGraph and can take a while; don't open the UI before the API answers.
printf "→ Waiting for the backend"
for _ in $(seq 1 120); do
  if ! kill -0 "$BACK" 2>/dev/null; then
    echo; echo "✗ The backend exited during startup. Last lines of $LOG:"; tail -n 30 "$LOG"; exit 1
  fi
  if curl -fs "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    echo " ready."; break
  fi
  printf "."; sleep 1
done
curl -fs "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1 || { echo; echo "✗ Backend did not answer within 120s. See $LOG"; exit 1; }
grep -m1 "generated password" "$LOG" 2>/dev/null || true

echo "→ Open http://localhost:$FRONTEND_PORT"
cd frontend && npm run dev -- --port "$FRONTEND_PORT" --strictPort
