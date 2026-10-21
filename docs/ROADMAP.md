# CertVerify — Product Vision & Roadmap

## Vision

A secure web platform where students upload academic documents and get them **validated and verified
immediately**. The platform is fast, easy to use and smart, and it is **universal**: it supports any document type
it has been taught. It starts with Indian academic records (10th, 12th, Diploma, semester sheets, provisional and
degree certificates) and grows into a global product.

- **Now:** the C-DAC Google Form export (`data_feb_26.csv`) plus its Drive files is the **seed dataset** used to build
  and measure the document engine. It is not a permanent dependency.
- **Later:** students upload directly on the platform, and the Google Form is retired.
- **Pillars:** storage · processing · validation/verification · security · user-friendliness.

## Architecture principles

1. **Document types are data, not code.** A *document-type registry* defines each type's fields, how to recognise it
   and how to extract and validate it. Adding a type should mean adding examples and config, not rewriting the system.
2. **Requirement sets per institution.** "C-DAC PG admission needs 10th, 12th/Diploma, all semesters, provisional/degree"
   is one configurable rule set. Other institutions define their own (multi-tenant).
3. **Pluggable engines.** OCR, classifier, extractor and LLM are interfaces. They are local and CPU-only today, and can
   be upgraded later (fine-tuned models, GPU, or cloud if ever allowed) without changing the rest.
4. **Human-in-the-loop learning.** Every verifier correction is stored as a labelled example. Those examples
   become training data, so the system gets smarter with use.
5. **Fast feedback, honest status.** Quality checks happen instantly on upload. OCR and extraction run in a background
   worker with live progress. Low-confidence results go to a human; the system never guesses silently.
6. **Security by default.** Least-privilege roles, tenant isolation, encryption of stored files, audit trail for every
   view and change, and PII never in logs or URLs.

## Technology stack

| Layer | Choice |
|---|---|
| API | Python 3.12, FastAPI, Pydantic |
| Database | SQLite (dev) → PostgreSQL; SQLAlchemy 2.0 + Alembic |
| File storage | Storage interface: encrypted local disk (dev) → S3-compatible (MinIO/S3) |
| Background jobs | Redis + worker queue (RQ/Celery), introduced when processing goes async |
| Imaging / OCR | pypdfium2 (permissive licence; PyMuPDF is AGPL), OpenCV, Pillow + pillow-heif, Tesseract 5 (rotation, fallback) + PaddleOCR PP-OCRv6 (main reading; separate worker process and venv) |
| Intelligence | Rules + registry config first; ML classifier and layout-aware extraction model trained on collected labels; local LLM (Ollama) as fallback |
| Frontend | React + TypeScript + Vite (student portal and verifier console) |
| Auth | Email/password + OTP, JWT sessions, RBAC (student, verifier, institution admin, platform admin) |
| Ops | Docker Compose, CI (tests + lint), backups, monitoring |

## Roadmap

### Stage A: Platform foundation
| # | Phase | Delivers |
|---|---|---|
| 1 | Foundation ✅ | App skeleton, config, logging, DB engine, health check, tests |
| 2 | Core domain model + secure storage ✅ | Institutions, users/roles (tables only), applicants, document-type registry, requirement sets, documents, pages, extracted fields, issues, audit log; Alembic migrations; storage service with encryption at rest and content hashing |
| 3 | Seed dataset ingestion ✅ | Form CSV importer (82 students) + local-folder importer for the Drive export (574 files, name-based mapping with human confirmation); optional Drive API downloader kept for later intakes |

### Stage B: Document intelligence engine (built and measured offline on the seed data)
| # | Phase | Delivers |
|---|---|---|
| 4 | Intake quality gate + preprocessing ✅ | Real file type, HEIC/MPO/EXIF handling, padded-PDF repair, page rendering at source resolution, margin trim + small-skew correction, separate quality checks (resolution, blank, contrast, extreme blur) with student-facing reasons; originals never modified. 90° rotation moves to Phase 5 (Tesseract OSD) |
| 5 | OCR + evaluation harness ✅ | Tesseract with background removal + sparse-text mode, rotation chosen by OCR confidence (40/40 on labelled pages), words with positions and confidence stored per page, readability status (ok / low_confidence / no_text / failed); `make ocr-eval` on a 40-page, 237-key hand-labelled set: 75.5% key recall (words 92%, numbers 53%) |
| 6 | Classification ✅ | Rule-based, OCR-tolerant page types (10th, 12th, diploma, semester, provisional/passing, degree, supporting page) with semester(s), school/UG/PG level, confidence and evidence; continuation and slot-hint fallbacks; 98%/95% type accuracy on tuning/held-out labelled pages, all errors below high confidence; 3.8% unknown → review |
| 7 | Extraction ✅ | Per-type field schemas (name, mother's name, IDs, year, marks, %, SGPA/CGPA, result). The seed data has ~10 school/diploma boards and ~25 universities, so extraction is generic first (OCR key–value + table parsing, local LLM fallback), with dedicated rules only for the most common sources (MSBSHSE, CBSE, MSBTE, SPPU, Mumbai, DBATU); per-field confidence; must compensate for weak digit OCR (label-anchored reading of word boxes, re-OCR of number regions with a digit whitelist, cross-checks such as total = sum of marks). PaddleOCR worker added: numbers recall 53% → 89% on the labelled set. Done: generic label/position finders with OCR repair and cross-checks, form-name matching, per-page fields with confidence and position; 87% of held-out labelled values correct, 98% of those with confidence ≥ 0.9. Later: university/board, programme and exam session; local LLM fallback |
| 8 | Validation & verification rules engine | Configurable rules: completeness per requirement set, exact + content duplicates, cross-document identity, semester sequence/gaps, claimed % vs per-university CGPA formula, QR/serial-number checks where the document supports them |

### Stage C: Product
| # | Phase | Delivers |
|---|---|---|
| 9 | Authentication & security | Accounts, OTP, sessions, RBAC, tenant isolation, rate limiting, upload hardening, audit of every access |
| 10 | Async processing pipeline | Redis worker queue; upload → quick checks (seconds) → OCR/classify/extract/validate in the background; live status to the browser |
| 11 | Student portal | Guided checklist per requirement set; drag-and-drop or phone-camera upload; instant quality feedback; review of extracted data; clear "what to fix" messages |
| 12 | Verifier console | Queue with issue badges; document viewer next to the extracted fields; correct, approve or reject; message the student; every correction saved as a training label |
| 13 | Learning loop & universal types | Dataset export from corrections; train an ML classifier and then a layout-aware extractor; model versioning and evaluation; add a new document type from the admin UI |

### Stage D: Launch & scale
| # | Phase | Delivers |
|---|---|---|
| 14 | Production infrastructure | PostgreSQL, Redis, S3/MinIO, Docker Compose deployment, CI/CD, backups, monitoring, load test |
| 15 | Compliance & global readiness | Consent, retention and deletion (India DPDP Act 2023, GDPR), privacy policy, i18n, issuer verification integrations (e.g. DigiLocker / NAD) |
| 16 | Pilot & cutover | Run a real C-DAC intake on the platform, compare with manual checking, retire the Google Form |

Each phase ends with passing tests, a manual check by the user and the user's confirmation before the next one starts.
Dependencies are installed only at the start of the phase that needs them.

## Known limits & open decisions

- **"Universal" grows with data.** Each new document layout needs examples (ideally dozens) to be recognised reliably.
  Unseen types are flagged for human review, not guessed.
- **Speed on current hardware.** Quality checks take about 1–2 s. OCR + extraction takes a few seconds per page on CPU.
  Fine for a pilot; production capacity is decided in Phase 14.
- **Authenticity.** Consistency checks catch errors and many edits, but a photo alone cannot prove a document is
  genuine. Strong verification needs issuer sources (QR codes, DigiLocker/NAD, university portals). Planned in Phase 15.
- **Local-only processing** holds for now. Whether a hosted product may use cloud AI is a business decision for Phase 14.
- **Consent for training data.** The seed documents were collected for C-DAC admission. Using them to train a
  commercial product should be cleared with the data owner (C-DAC) first.
