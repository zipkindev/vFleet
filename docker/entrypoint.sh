#!/bin/sh
set -eu

if [ -z "${UI_TOKEN:-}" ]; then
  echo "Refusing to start: the container image requires a non-empty UI_TOKEN." >&2
  echo "Publish port 8080 to loopback and send the same token from the vFleet UI." >&2
  exit 64
fi

exec python -m uvicorn app.main:app \
  --app-dir /opt/vfleet/backend \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1 \
  --no-access-log
