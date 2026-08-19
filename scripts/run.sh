#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

if [[ ! -d backend/.venv ]]; then
  python3 -m venv backend/.venv
fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install -q -r backend/requirements.txt

(cd frontend && npm install && npm run build)

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8081}"
echo "Serving vFleet at http://${HOST}:${PORT}"
uvicorn app.main:app --app-dir backend --host "$HOST" --port "$PORT"
