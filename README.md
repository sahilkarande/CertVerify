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

Back up `backend/data/certverify.db` before migrating real data. On SQLite, migrations run with foreign keys
disabled (table rebuilds would otherwise cascade-delete child rows) and end with an integrity check.

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

## Document processing (intake & quality)

```bash
make process            # render, clean and quality-check documents not yet processed (safe to re-run)
make process force=1    # re-process everything
make quality-report     # page verdicts (ok / warn / retake / blank) per upload slot
```

## OCR

Needs the system package: `sudo apt install tesseract-ocr tesseract-ocr-eng`.

```bash
make ocr                # read text from processed pages (safe to re-run; resumes if interrupted)
make ocr force=1        # re-run OCR on everything
make ocr-eval           # measure recall on the hand-labelled sample in backend/data/ocr_eval/
```

### PaddleOCR (more accurate, recommended for extraction)

PaddleOCR runs in its own environment (its numpy/OpenCV versions conflict with the app's) as a
worker process the app talks to over a pipe. Tesseract still decides each page's rotation; PaddleOCR
then reads the upright page once. When a page has both readings, later steps use PaddleOCR's.

```bash
make paddle-setup              # one-time, ~1.4 GB; models (~50 MB) download on first use
make ocr engine=paddle         # needs make ocr first (reuses its rotations); ~5 s per page
make ocr-eval engine=paddle
make classify force=1          # re-classify on the better text
```

Model choice: `CERTVERIFY_PADDLE_DET_MODEL` / `CERTVERIFY_PADDLE_REC_MODEL` (default `PP-OCRv6_tiny_*`;
`PP-OCRv6_small_*` is ~4 points more accurate and 3x slower). The worker's real-model test is opt-in:
`CERTVERIFY_TEST_PADDLE=1 make test`.

## Page classification

```bash
make classify                # type, semester and level for every OCR'd page (safe to re-run)
make classify-eval           # score against the labelled pages in backend/data/ocr_eval/
make classification-report   # detected types per upload slot (shows wrong-slot uploads)
```

Pages with confidence below 0.9 (fuzzy title match, continuation or slot hint) should be checked by a person.

## Field extraction

```bash
make extract              # names, IDs, SGPA/CGPA, percentage, total, result for every classified page
make extract force=1      # re-extract everything (reviewer corrections are always kept)
make extract-eval         # score on the labelled tuning set and the held-out set (backend/data/ocr_eval/)
make extraction-report    # how many pages of each type have each field (counts only)
```

Values are stored per page (`extracted_fields`) with a confidence and their position. Confidence ≥ 0.9 was
right on 98% of held-out values; anything lower should be checked by a person. Needs `make classify` first.

## Validation

```bash
make validate             # check every submission; stores issues (safe to re-run)
make validation-report    # open issues by type and severity (counts only)
```

Issues are `error` (very likely wrong), `warning` (a person must look) or `info`. Automatic checks never mark a
submission verified; submissions with warnings or errors move to `needs_review`. An issue a verifier dismissed
stays dismissed on later runs. Needs `make extract` first; `make import-form` refreshes the requirement set.

## Review dashboard

A read-only screen for verifiers: every applicant with a verdict (clear / needs review / problems), and for each one
the issues, every uploaded page (upright, with the extracted values outlined on the page and coloured by
confidence), the page type, quality and OCR details. Needs Node.js 20.19+.

```bash
make frontend-install    # once
make frontend-build      # after changes to frontend/
make run                 # then open http://127.0.0.1:8000/
```

## Verifier console

On an applicant's review screen, staff can:
- **Values:** click a value to correct it, ✓ to confirm it is right, or "Add a value the machine missed". The
  machine's original reading is kept next to the verified value.
- **Issues:** mark an issue resolved, or dismiss it. Dismissing needs a written reason, which is kept on record, and
  any issue can be reopened.
- **Decide:**
  - **Approve:** only possible once every error and warning is resolved or dismissed.
  - **Request changes:** with a message; the application goes back to the student, who can fix and submit again.
  - **Reject:** with a reason.
  - **Reopen a decision:** with a reason.
- **Message the student:** messages are shown in their portal and emailed, and the student can reply.

Every action is audited (who and what, never the values). Edits made from an out-of-date screen are refused instead of
overwriting someone else's change.

**Learning loop:** every confirmed, corrected or added value is a labelled example.

```bash
make corrections-report    # per value type: how often the machine was right, corrected or missed (real-use accuracy)
make export-corrections    # training data: backend/data/training/corrections-<date>.jsonl (personal data, private)
```

## Learning loop & new document types

**Page-type model.** A text classifier is trained on pages the rules recognised by an exact title, plus page types
set by verifiers. It is never trained on the hand-labelled evaluation pages, and it is stored as plain JSON numbers
(no pickle files), versioned, and checked against its recorded hash on every load.

```bash
make train-classifier          # new version, scored on the hand labels (needs: pip install -e ".[train]")
make models                    # versions and scores
make activate-model version=1  # use it (without version: switch it off)
make classify force=1          # re-classify with it
```

How the model is used:
1. It types pages the rules cannot recognise, when it is at least 60% sure.
2. It overrules a "same as previous page" guess only when it is at least 80% sure.
3. It lowers the confidence of an exact rule match it strongly disagrees with.

Model-typed pages are capped at 0.85 confidence, so a person always checks them. Verifiers can set a page's type in
the console; that type is kept on re-classification and used as a training label.

**New document types without code** (Settings, platform admins): a name, the values to read (chosen from what the
extractor can find, plus values verifiers type in) and title phrases from the page header. Pages whose header contains
a phrase get that type. **Checklists** (Settings, institution admins for their own institution): mark documents as
required or not, change the accepted types, add documents.

## Student portal

Students open the same address and choose "I'm a student":
1. They enter their email and get a 6-digit code by email. There is no password. The code works once, for 10 minutes,
   with 5 tries.
2. They answer a few questions (name, phone, degree status, aggregate %).
3. They upload each required document from a checklist, with "Take photo" (phone camera) or "Choose file". The
   checklist adapts to their answers; for example "awaiting provisional" asks for the final-semester marksheet.
4. Each document is checked in the background, and its card shows "Looks good" or exactly what to fix ("The image is
   too blurry…", "This document is missing…") within seconds.
5. They can remove a wrong upload, and submit once every required document is there. Documents can still be replaced
   until a verifier has decided.

Applicants imported from the Google Form find their existing application by signing in with the same email.
Students never see checks meant for verifiers (files or IDs shared with another applicant, claimed-percentage doubts).

**Email:** during development, codes are written to `backend/data/outbox/*.eml` (readable only by you) instead of
being sent. To send real email, for example from Gmail with a Google app password, put this in `backend/.env`:

```
CERTVERIFY_EMAIL_BACKEND=smtp
CERTVERIFY_EMAIL_FROM=CertVerify <your.address@gmail.com>
CERTVERIFY_SMTP_USERNAME=your.address@gmail.com
CERTVERIFY_SMTP_PASSWORD=the 16-character app password
```

## Background processing

Uploaded documents are processed in the background by a worker:
quality check → OCR (Tesseract rotation + PaddleOCR reading) → classification → extraction → validation.
The queue is a table in the database, so there is no extra service to install.

```bash
make run            # terminal 1: web app + API
make worker         # terminal 2: processes queued documents (Ctrl+C finishes the current one, then stops)
make enqueue        # queue documents whose processing is out of date (all=1: everything)
make queue-status   # job counts, and failures by error type
```

- Staff can add a document to an applicant on the review screen ("Add a document"), and its progress is shown live.
- Upload rules:
  - At most 10 MB.
  - PDF, JPEG, PNG, HEIC or WEBP, detected from the file content (names and extensions are not trusted).
  - At most 40 pages and 60 megapixels per image.
- Failed steps are retried after 30 s and then 5 min. After 3 failed attempts the job is marked failed.
- A worker that crashes or sleeps leaves its job to another worker after 10 minutes.
- An unreadable file is not retried: it becomes a "file unreadable" issue for the uploader to fix.

## Staff accounts and sign-in

Staff sign in with email + password, then a 6-digit code from an authenticator app (Google/Microsoft Authenticator,
etc.), which is set up by scanning a QR code at the first sign-in.

```bash
make create-user email=you@example.org name="Your Name" role=platform_admin   # asks for a password (12+ chars)
make create-user email=v@cdac.in name="A Verifier" role=verifier temp=1      # prints a one-time password
make list-users
make reset-2fa email=v@cdac.in        # lost phone: set up a new authenticator at next sign-in
make set-password email=v@cdac.in     # also signs the user out everywhere
make disable-user email=v@cdac.in     # and enable-user (also clears a lockout)
```

- Roles:
  - **verifier** and **institution_admin** see their own institution's applicants.
  - **platform_admin** sees all institutions.
  - Another institution's records answer "not found".
- Protections:
  - Passwords are stored as Argon2id hashes, and authenticator secrets are encrypted.
  - Each sign-in creates a revocable server-side session: an HttpOnly, SameSite=Strict cookie, Secure in production.
  - Sessions end after 2 h idle or 12 h at most.
  - 5 wrong attempts lock the account for 15 min, doubling with each further failure; there are per-address limits.
  - Authenticator codes cannot be reused.
  - Changes require a same-site request header (CSRF).
  - Security headers and a strict Content Security Policy are sent on every response.
- Every sign-in event and every opened applicant or page image is in the audit log, with who did it.
- Student accounts come with the student portal (Phase 11).

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
    intake/          # open uploads, render pages, cleanup, quality checks, processing pipeline
    ocr/             # OCR engine interface (Tesseract, PaddleOCR worker client), rotation, evaluation
    classify/        # page type, semester and level rules, fallbacks, evaluation
    extract/         # page layout, value parsers, field finders, storage, evaluation
    validate/        # submission context, validation rules, issue storage
    review/          # read models for the review dashboard
    api/             # router, dependencies, routes/
  migrations/        # Alembic migrations
  workers/paddle_ocr/  # PaddleOCR worker (runs in backend/.paddle-venv, not the app venv)
frontend/            # review dashboard (React + TypeScript + Vite)
  tests/
docs/
```
