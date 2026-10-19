VENV := backend/.venv
PY   := $(VENV)/bin/python

.PHONY: setup run test lint format migrate seed revision import-form drive-auth drive-download import-status

setup:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	cd backend && .venv/bin/pip install -e ".[dev,gdrive]"

run:
	cd backend && .venv/bin/uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

test:
	cd backend && .venv/bin/pytest

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

format:
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

# Apply all database migrations.
migrate:
	cd backend && .venv/bin/alembic upgrade head

# Insert/update the document-type registry (idempotent).
seed:
	cd backend && .venv/bin/python -m app.cli seed

# Create a new migration from model changes: make revision m="describe change"
revision:
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

# --- Seed data import (Phase 3) ---
# make import-form csv=../data_feb_26.csv   (path relative to backend/)
import-form:
	cd backend && .venv/bin/python -m app.cli import-form "$(csv)"

# One-time Google sign-in (opens a browser)
drive-auth:
	cd backend && .venv/bin/python -m app.cli drive-auth

# make drive-download [limit=5] [retry=1]
drive-download:
	cd backend && .venv/bin/python -m app.cli drive-download $(if $(limit),--limit $(limit)) $(if $(retry),--retry-failed)

import-status:
	cd backend && .venv/bin/python -m app.cli import-status
