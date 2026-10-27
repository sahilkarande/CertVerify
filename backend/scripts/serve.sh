#!/usr/bin/env bash
# The web app and the background worker together (make run / make run-lan): uploads are
# only processed while a worker runs, and two separate commands were easy to forget.
# Ctrl+C stops the web server; the worker finishes the document it is on, then stops.
# Usage: scripts/serve.sh HOST [no-worker]
set -u
cd "$(dirname "$0")/.."
host=${1:-127.0.0.1}
port=${PORT:-8000}
worker=""
if [ "${2:-}" != "no-worker" ]; then
  # Own session, so Ctrl+C reaches only the web server and the worker gets one clean stop.
  setsid .venv/bin/python -m app.jobs.worker &
  worker=$!
  trap 'echo "stopping the worker after its current document..."; kill -TERM "$worker" 2>/dev/null; wait "$worker"' EXIT
fi
.venv/bin/uvicorn app.main:create_app --factory --reload --host "$host" --port "$port"
