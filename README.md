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

## Seed data import (C-DAC Google Form)

The form export (`data_feb_26.csv`) and the documents it links to are the seed dataset.
Everything lands in the git-ignored `backend/data/`.

```bash
make import-form csv=../data_feb_26.csv   # students, submissions, Drive file refs (safe to re-run)
make import-status                         # progress summary
```

### Importing the downloaded Drive folder (used for the Feb 2026 seed data)

Download the form's "File responses" folder from Drive into the project root (it is git-ignored), then:

```bash
# 1. Dry run: matches each Google account name in the file names to a form row and writes
#    backend/data/folder_mapping.csv (git-ignored, contains names)
make import-folder dir="../PGCP-BDA  - FEB -26 (File responses)"
# 2. Open the mapping file, check every row with confirmed=no, set it to yes (fix form_number if needed)
# 3. Import (safe to re-run; already imported files are skipped)
make import-folder dir="../PGCP-BDA  - FEB -26 (File responses)" apply=1
```

### Google Drive download (optional alternative): one-time setup

The files are shared *to* your Google account, so the app signs in as you with **read-only** access.

1. Open https://console.cloud.google.com/ and create a project (e.g. `CertVerify`).
2. **APIs & Services → Library**: search for **Google Drive API** and click **Enable**.
3. **Google Auth Platform → Branding / Audience** (older UIs: *OAuth consent screen*):
   - App name `CertVerify`, your email as support and developer contact.
   - Audience **External**, publishing status **Testing**.
   - Under **Test users**, add the Google account the files are shared with.
4. **Google Auth Platform → Clients → Create client** (older UIs: *Credentials → Create OAuth client ID*):
   - Application type **Desktop app**, name `CertVerify CLI`.
   - Download the JSON and save it as `backend/data/google_client_secret.json`.
5. Sign in once. A browser opens; choose the account and allow read-only Drive access:
   ```bash
   make drive-auth
   ```
   You will see a "Google hasn't verified this app" warning. That is expected for your own app in
   testing mode: choose *Continue*. The token is saved to `backend/data/google_token.json`.
6. Download:
   ```bash
   make drive-download limit=5     # try a few first
   make drive-download             # the rest; safe to stop and re-run
   make drive-download retry=1     # retry files that failed (e.g. after access was granted)
   ```

In *Testing* mode Google expires the sign-in after 7 days. If that happens, run `make drive-auth` again.
Never commit `google_client_secret.json` or `google_token.json` (both are git-ignored).

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
    importers/       # one-off seed importers (C-DAC form CSV, Google Drive)
    api/             # router, dependencies, routes/
  migrations/        # Alembic migrations
  tests/
docs/
```
