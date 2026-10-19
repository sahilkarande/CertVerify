# CertVerify

AI-powered Student Academic Document Management System. See [docs/ROADMAP.md](docs/ROADMAP.md) for the stack and phase plan.

## Requirements

- Python 3.12+
- `make` (optional; the commands it wraps are listed below)

## Setup

```bash
make setup                         # creates backend/.venv and installs deps
cp backend/.env.example backend/.env   # optional
```

## Run

```bash
make run
```

- Health check: http://127.0.0.1:8000/api/v1/health
- API docs: http://127.0.0.1:8000/docs

## Test & lint

```bash
make test
make lint
```

## Layout

```
backend/
  app/
    main.py          # app factory + ASGI entrypoint
    core/            # settings, logging
    db/              # engine/session, declarative base
    api/             # router, dependencies, routes/
  tests/
docs/ROADMAP.md
```
