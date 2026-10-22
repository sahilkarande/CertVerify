VENV := backend/.venv
PY   := $(VENV)/bin/python

.PHONY: setup run test lint format migrate seed revision import-form import-folder drive-auth drive-download import-status process quality-report ocr ocr-eval paddle-setup classify classify-eval classification-report extract extract-eval extraction-report validate validation-report frontend-install frontend-dev frontend-build create-user list-users reset-2fa set-password disable-user enable-user worker enqueue queue-status

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

# Import files from the downloaded Drive folder (path relative to backend/).
# Step 1 (dry run, writes backend/data/folder_mapping.csv):  make import-folder dir="../PGCP-BDA  - FEB -26 (File responses)"
# Step 2 (after reviewing the mapping):                     make import-folder dir="..." apply=1
import-folder:
	cd backend && .venv/bin/python -m app.cli import-folder "$(dir)" $(if $(apply),--apply)

# One-time Google sign-in (opens a browser)
drive-auth:
	cd backend && .venv/bin/python -m app.cli drive-auth

# make drive-download [limit=5] [retry=1]
drive-download:
	cd backend && .venv/bin/python -m app.cli drive-download $(if $(limit),--limit $(limit)) $(if $(retry),--retry-failed)

import-status:
	cd backend && .venv/bin/python -m app.cli import-status

# --- Intake & preprocessing (Phase 4) ---
# make process [limit=10] [force=1]
process:
	cd backend && .venv/bin/python -m app.cli process $(if $(limit),--limit $(limit)) $(if $(force),--force)

quality-report:
	cd backend && .venv/bin/python -m app.cli quality-report

# --- OCR (Phase 5; needs: sudo apt install tesseract-ocr) ---
# make ocr [engine=paddle] [limit=10] [force=1]
ocr:
	cd backend && .venv/bin/python -m app.cli ocr $(if $(engine),--engine $(engine)) $(if $(limit),--limit $(limit)) $(if $(force),--force)

# Measure OCR on the hand-labelled sample (backend/data/ocr_eval/ground_truth.json)
# make ocr-eval [engine=paddle]
ocr-eval:
	cd backend && .venv/bin/python -m app.cli ocr-eval $(if $(engine),--engine $(engine))

# PaddleOCR in its own venv (its numpy/OpenCV pins conflict with the app's). ~1.4 GB.
paddle-setup:
	python3 -m venv backend/.paddle-venv
	backend/.paddle-venv/bin/pip install -r backend/workers/paddle_ocr/requirements.txt

# --- Page classification (Phase 6) ---
# make classify [force=1]
classify:
	cd backend && .venv/bin/python -m app.cli classify $(if $(force),--force)

# Score against the hand-labelled pages (backend/data/ocr_eval/classification.json)
classify-eval:
	cd backend && .venv/bin/python -m app.cli classify-eval

classification-report:
	cd backend && .venv/bin/python -m app.cli classification-report

# --- Field extraction (Phase 7) ---
# make extract [force=1]
extract:
	cd backend && .venv/bin/python -m app.cli extract $(if $(force),--force)

# Score against labelled pages: tuning set + held-out set (backend/data/ocr_eval/)
extract-eval:
	cd backend && .venv/bin/python -m app.cli extract-eval

extraction-report:
	cd backend && .venv/bin/python -m app.cli extraction-report

# --- Validation (Phase 8) ---
# Checks every submission and stores issues (safe to re-run; dismissed issues stay dismissed)
validate:
	cd backend && .venv/bin/python -m app.cli validate

validation-report:
	cd backend && .venv/bin/python -m app.cli validation-report

# --- Review dashboard (read-only part of Phase 12, built early) ---
# One-time: install the dashboard's packages (frontend/node_modules)
frontend-install:
	cd frontend && npm ci

# Build once; `make run` then serves the dashboard at http://127.0.0.1:8000/
frontend-build:
	cd frontend && npm run build

# While editing the dashboard: http://127.0.0.1:5173 with live reload (needs `make run` too)
frontend-dev:
	cd frontend && npm run dev

# --- Staff accounts (Phase 9) ---
# make create-user email=you@example.org name="Your Name" role=verifier [institution=cdac-kharghar] [temp=1]
#   role: verifier | institution_admin | platform_admin ; asks for the password unless temp=1
create-user:
	cd backend && .venv/bin/python -m app.cli create-user "$(email)" --name "$(name)" --role $(role) $(if $(institution),--institution $(institution)) $(if $(temp),--temporary-password)

list-users:
	cd backend && .venv/bin/python -m app.cli list-users

# Lost phone: remove the authenticator; a new one is set up at the next sign-in
reset-2fa:
	cd backend && .venv/bin/python -m app.cli reset-2fa "$(email)"

set-password:
	cd backend && .venv/bin/python -m app.cli set-password "$(email)" $(if $(temp),--temporary-password)

disable-user:
	cd backend && .venv/bin/python -m app.cli disable-user "$(email)"

enable-user:
	cd backend && .venv/bin/python -m app.cli enable-user "$(email)"

# --- Background processing (Phase 10) ---
# Processes uploaded/queued documents: quality -> OCR -> classify -> extract -> validate.
# Run alongside `make run`; Ctrl+C finishes the current document, then stops.
worker:
	cd backend && .venv/bin/python -m app.jobs.worker

# Queue documents for the worker: out-of-date ones, or everything with all=1
enqueue:
	cd backend && .venv/bin/python -m app.cli enqueue $(if $(all),--all)

queue-status:
	cd backend && .venv/bin/python -m app.cli queue-status
