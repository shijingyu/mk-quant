#!/usr/bin/env bash
# Local demo: each service is still an independent OS process.
set -euo pipefail
cd "$(dirname "$0")/.."
MK_PYTHON="${MK_PYTHON:-.venv/bin/python}"
if [ ! -x "$MK_PYTHON" ]; then
  echo 'Python environment missing. See README.md for installation.' >&2
  exit 1
fi
export MK_DEMO="${MK_DEMO:-1}"
export MK_DB_PATH="${MK_DB_PATH:-data/mk.sqlite3}"
if [ "$MK_DEMO" = "1" ]; then
  if [ -n "${MK_DEMO_PASSWORD:-}" ]; then
    "$MK_PYTHON" -m backend.worker init-demo
  else
    "$MK_PYTHON" -m backend.worker init-demo --generate-password
  fi
fi
"$MK_PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 &
MK_API_PID=$!
"$MK_PYTHON" -m backend.worker backtest &
MK_BT_PID=$!
"$MK_PYTHON" -m backend.worker runtime &
MK_RT_PID=$!
npm run dev --prefix frontend &
MK_WEB_PID=$!
cleanup() { kill "$MK_API_PID" "$MK_BT_PID" "$MK_RT_PID" "$MK_WEB_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
wait
