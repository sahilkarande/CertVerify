# CertVerify

AI-powered student academic document verification platform.
See [docs/ROADMAP.md](docs/ROADMAP.md) for the plan and [docs/DATA_ANALYSIS.md](docs/DATA_ANALYSIS.md) for the seed data analysis.

## Requirements

- Python 3.12+
- `make` (optional; the commands it wraps are in the `Makefile`)

## Setup

```bash
make setup                             # creates backend/.venv and installs deps
cp backend/.env.example backend/.env   # optional
make migrate                           # create/upgrade database tables
make seed                              # load the document-type registry (safe to re-run)
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

## Database changes

Edit the models in `backend/app/models/`, then:

```bash
make revision m="describe the change"   # autogenerate a migration, then review it
make migrate
```

## File encryption key

Uploaded documents are encrypted at rest (AES-256-GCM).

- **Development:** a key is generated once at `backend/data/.storage.key`. **Back it up.** Losing it makes every stored file unreadable.
- **Production:** set `CERTVERIFY_STORAGE_KEY`. Generate a value with `cd backend && .venv/bin/python -m app.cli generate-key`. The app refuses to start without it.

## Layout

```
backend/
  app/
    main.py          # app factory (run with uvicorn --factory)
    cli.py           # management commands (seed, generate-key)
    core/            # settings, logging, key loading
    db/              # engine/session, declarative base
    models/          # ORM models (tenancy, registry, applicants, documents, review)
    storage/         # encrypted object storage
    services/        # file storage + audit helpers
    seeds/           # document-type registry definitions
    api/             # router, dependencies, routes/
  migrations/        # Alembic migrations
  tests/
docs/
```
