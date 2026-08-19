#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example (demo mode)."
fi

if [[ ! -d backend/.venv ]]; then
  python3 -m venv backend/.venv
fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install -q -r backend/requirements.txt

if [[ ! -d frontend/node_modules ]]; then
  (cd frontend && npm install)
fi

cleanup() {
  kill "$API_PID" "$UI_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8081 &
API_PID=$!

(cd frontend && npm run dev -- --host 127.0.0.1 --port 5173) &
UI_PID=$!

echo
echo "vFleet UI:  http://127.0.0.1:5173"
echo "API:        http://127.0.0.1:8081/api/health"
echo "Port 8080 is often Homebrew nginx; this stack uses 8081 for the API."
echo "Fill .env with VCENTER_* to switch from demo to live inventory."
echo

wait
