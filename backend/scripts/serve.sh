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
  # Restarted when the code changes, like the web server: a worker still running old code
  # fails on anything new (it once failed every Testing Lab upload). It is given up to two
  # minutes to finish the document it is on.
  setsid .venv/bin/watchfiles --filter python --sigint-timeout 120 \
    ".venv/bin/python -m app.jobs.worker" app &
  worker=$!
  trap 'echo "stopping the worker after its current document..."; kill -TERM "$worker" 2>/dev/null; wait "$worker"' EXIT
fi
.venv/bin/uvicorn app.main:create_app --factory --reload --host "$host" --port "$port"
