"""Management commands: `python -m app.cli <command>`."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import generate_key, load_storage_key
from app.db.session import Database
from app.seeds.document_types import seed_document_types


def _database() -> Database:
    return Database(get_settings().database_url)


def cmd_seed(_: argparse.Namespace) -> int:
    db = _database()
    with db.session_factory() as session, session.begin():
        created, updated = seed_document_types(session)
    db.dispose()
    print(f"document types: {created} created, {updated} updated")
    return 0


def cmd_generate_key(_: argparse.Namespace) -> int:
    print(generate_key())
    return 0


def cmd_import_form(args: argparse.Namespace) -> int:
    from app.importers.cdac_form.importer import import_form
    from app.importers.cdac_form.parser import FormFormatError, parse_csv

    path = Path(args.csv)
    if not path.is_file():
        print(f"error: CSV not found: {path}", file=sys.stderr)
        return 2
    try:
        parsed = parse_csv(path)
    except FormFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = _database()
    with db.session_factory() as session, session.begin():
        report = import_form(session, parsed)
    db.dispose()

    print(
        f"rows: {report.rows} | applicants created: {report.applicants_created}, "
        f"updated: {report.applicants_updated} | Drive file refs created: "
        f"{report.file_refs_created}, already known: {report.file_refs_existing}"
    )
    if report.warnings:
        print(f"{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  - {w}")
    return 0


def cmd_drive_auth(_: argparse.Namespace) -> int:
    from app.importers.gdrive.client import AuthRequiredError, load_credentials

    settings = get_settings()
    try:
        load_credentials(
            settings.google_client_secret_file, settings.google_token_file, interactive=True
        )
    except AuthRequiredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Signed in. Token saved to {settings.google_token_file}")
    return 0


def cmd_drive_download(args: argparse.Namespace) -> int:
    from app.importers.gdrive.client import AuthRequiredError, GoogleDriveClient, load_credentials
    from app.importers.gdrive.downloader import download_pending
    from app.storage import EncryptedLocalStorage

    settings = get_settings()
    try:
        creds = load_credentials(
            settings.google_client_secret_file, settings.google_token_file, interactive=False
        )
    except AuthRequiredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    storage = EncryptedLocalStorage(settings.storage_dir, load_storage_key(settings))
    client = GoogleDriveClient(creds)
    db = _database()
    with db.session_factory() as session:
        report = download_pending(
            session, storage, client, limit=args.limit, retry_failed=args.retry_failed
        )
    db.dispose()
    print(
        f"fetched: {report.fetched} ({report.bytes_downloaded / 1_048_576:.1f} MB) | "
        f"failed: {report.failed}"
    )
    for e in report.errors[:20]:
        print(f"  - {e}")
    if len(report.errors) > 20:
        print(f"  ... and {len(report.errors) - 20} more (see `import-status`)")
    return 0 if report.failed == 0 else 1


def cmd_import_folder(args: argparse.Namespace) -> int:
    from app.importers.cdac_form.folder import (
        FolderImportError,
        apply_import,
        plan_mapping,
        read_mapping,
        scan_folder,
        write_mapping,
    )

    settings = get_settings()
    mapping_file = Path(args.mapping) if args.mapping else settings.folder_mapping_file
    try:
        scanned = scan_folder(Path(args.folder))
    except FolderImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    n_files = sum(len(p) for slots in scanned.files.values() for p in slots.values())
    print(f"folder: {len(scanned.files)} respondents, {n_files} files")
    for w in scanned.warnings:
        print(f"  - {w}")

    db = _database()
    try:
        with db.session_factory() as session:
            if not args.apply:
                rows = plan_mapping(session, scanned)
                session.rollback()
                write_mapping(mapping_file, rows)
                methods = Counter(r.method for r in rows)
                pending = sum(not r.confirmed for r in rows)
                print("matching: " + ", ".join(f"{m}={n}" for m, n in methods.most_common()))
                print(f"mapping written to {mapping_file}")
                if pending:
                    print(
                        f"{pending} row(s) need your review: open the file, check them, set "
                        "confirmed=yes (fix form_number if wrong), then re-run with apply=1"
                    )
                else:
                    print("all rows confirmed automatically; re-run with apply=1 to import")
                return 0

            report = apply_import(session, _storage(settings), scanned, read_mapping(mapping_file))
    except FolderImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.dispose()

    print(
        f"students: {report.students} | files stored: {report.files_stored}, already present: "
        f"{report.files_already_present} | Drive links marked fetched: "
        f"{report.drive_refs_linked}, relinked: {report.drive_refs_relinked}"
    )
    print("stored by type: " + ", ".join(f"{m}={n}" for m, n in sorted(report.by_mime.items())))
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    import time

    from app.intake.pipeline import PROCESSING_VERSION, process_document, summarize_verdicts
    from app.models import Document

    settings = get_settings()
    storage = _storage(settings)
    db = _database()
    results, failed, skipped = [], Counter(), 0
    started = time.perf_counter()
    with db.session_factory() as session:
        query = select(Document.id).order_by(Document.created_at, Document.id)
        if not args.force:
            query = query.where(
                (Document.processing_version.is_(None))
                | (Document.processing_version != PROCESSING_VERSION)
            )
        if args.limit:
            query = query.limit(args.limit)
        ids = list(session.scalars(query))
        for n, doc_id in enumerate(ids, start=1):
            doc = session.get(Document, doc_id)
            result = process_document(session, storage, doc, force=args.force)
            if result is None:
                skipped += 1
                continue
            results.append(result)
            if result.error_code:
                failed[result.error_code] += 1
            if n % 25 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} documents", flush=True)
    db.dispose()

    pages = sum(r.pages for r in results)
    secs = time.perf_counter() - started
    print(
        f"processed: {len(results)} documents, {pages} pages in {secs:.0f}s "
        f"({secs / max(pages, 1):.2f}s/page) | skipped (up to date): {skipped}"
    )
    print("page verdicts: " + ", ".join(f"{k}={v}" for k, v in summarize_verdicts(results).items()))
    if failed:
        print("failed documents: " + ", ".join(f"{k}={v}" for k, v in failed.items()))
    return 0


def cmd_quality_report(_: argparse.Namespace) -> int:
    from app.models import Document, DocumentPage

    db = _database()
    with db.session_factory() as session:
        rows = session.execute(
            select(
                Document.slot_key, DocumentPage.quality_verdict, DocumentPage.quality_reasons
            ).join(DocumentPage, DocumentPage.document_id == Document.id)
        ).all()
        notes = Counter(
            k for n in session.scalars(select(Document.processing_notes)) for k in (n or {})
        )
        errors = Counter(
            session.scalars(
                select(Document.processing_error).where(Document.processing_error.is_not(None))
            )
        )
    db.dispose()

    by_slot: dict[str, Counter] = {}
    reasons: Counter = Counter()
    for slot, verdict, rs in rows:
        by_slot.setdefault(slot or "-", Counter())[verdict.value if verdict else "none"] += 1
        reasons.update(rs or [])
    print(f"pages: {len(rows)}")
    for slot, c in sorted(by_slot.items()):
        print(f"  {slot:28} " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print("reasons: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
    print("intake notes: " + (", ".join(f"{k}={v}" for k, v in notes.items()) or "none"))
    print("failed documents: " + (", ".join(f"{k}={v}" for k, v in errors.items()) or "none"))
    return 0


def _ocr_engines(settings, name: str):
    """(engine, rotation_engine). PaddleOCR reads pages already turned upright by Tesseract,
    which picked the right rotation on 40/40 labelled pages."""
    from app.ocr.engine import TesseractEngine

    if name == "tesseract":
        return TesseractEngine(), None
    from app.ocr.paddle import PaddleEngine

    engine = PaddleEngine(
        settings.paddle_python,
        det_model=settings.paddle_det_model,
        rec_model=settings.paddle_rec_model,
    )
    return engine, TesseractEngine()


def cmd_ocr(args: argparse.Namespace) -> int:
    import time

    from app.models import DocumentPage
    from app.ocr.pipeline import ocr_page, pages_needing_ocr

    settings = get_settings()
    storage = _storage(settings)
    engine, rotation_engine = _ocr_engines(settings, args.engine)
    db = _database()
    statuses: Counter = Counter()
    rotated = 0
    started = time.perf_counter()
    with db.session_factory() as session:
        ids = pages_needing_ocr(session, engine.name, force=args.force)
        if args.limit:
            ids = ids[: args.limit]
        for n, page_id in enumerate(ids, start=1):
            page = session.get(DocumentPage, page_id)
            row = ocr_page(session, storage, engine, page, rotation_engine=rotation_engine)
            session.commit()  # per page: an interrupted run resumes where it stopped
            statuses[row.status.value] += 1
            rotated += row.rotation != 0
            if n % 25 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} pages", flush=True)
    db.dispose()
    if hasattr(engine, "close"):
        engine.close()
    secs = time.perf_counter() - started
    print(
        f"OCR ({engine.name}): {sum(statuses.values())} pages in {secs:.0f}s "
        f"({secs / max(sum(statuses.values()), 1):.1f}s/page) | rotated: {rotated}"
    )
    print("status: " + (", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) or "none"))
    return 0


def cmd_ocr_eval(args: argparse.Namespace) -> int:
    from app.ocr.evaluate import evaluate

    settings = get_settings()
    gt = Path(args.ground_truth) if args.ground_truth else settings.ocr_ground_truth_file
    if not gt.is_file():
        print(f"error: ground truth not found: {gt}", file=sys.stderr)
        return 2
    db = _database()
    with db.session_factory() as session:
        engine, rotation_engine = _ocr_engines(settings, args.engine)
        try:
            report = evaluate(
                session, _storage(settings), engine, gt, rotation_engine=rotation_engine
            )
        finally:
            if hasattr(engine, "close"):
                engine.close()
    db.dispose()
    for line in report.lines():
        print(line)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    import time

    from app.classify.pipeline import (
        classify_document,
        documents_needing_classification,
        type_id_map,
    )
    from app.models import Document

    db = _database()
    methods: Counter = Counter()
    started = time.perf_counter()
    with db.session_factory() as session:
        type_ids = type_id_map(session)
        ids = documents_needing_classification(session, force=args.force)
        for n, doc_id in enumerate(ids, start=1):
            result = classify_document(session, session.get(Document, doc_id), type_ids)
            session.commit()
            methods.update(result.methods)
            if n % 50 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} documents", flush=True)
    db.dispose()
    pages = sum(methods.values())
    print(
        f"classified: {len(ids)} documents, {pages} pages in {time.perf_counter() - started:.0f}s"
    )
    print("by method: " + (", ".join(f"{k}={v}" for k, v in sorted(methods.items())) or "none"))
    return 0


def cmd_classify_eval(args: argparse.Namespace) -> int:
    from app.classify.evaluate import evaluate

    settings = get_settings()
    labels = Path(args.labels) if args.labels else settings.classification_labels_file
    if not labels.is_file():
        print(f"error: labels not found: {labels}", file=sys.stderr)
        return 2
    db = _database()
    with db.session_factory() as session:
        report = evaluate(session, labels)
    db.dispose()
    for line in report.lines():
        print(line)
    return 0


def cmd_classification_report(_: argparse.Namespace) -> int:
    from app.models import Document, DocumentPage, DocumentType

    db = _database()
    with db.session_factory() as session:
        codes = dict(session.execute(select(DocumentType.id, DocumentType.code)).all())
        rows = session.execute(
            select(Document.slot_key, DocumentPage.detected_type_id)
            .join(DocumentPage, DocumentPage.document_id == Document.id)
            .where(DocumentPage.classifier_version.is_not(None))
        ).all()
    db.dispose()
    table: dict[str, Counter] = {}
    for slot, type_id in rows:
        table.setdefault(slot or "-", Counter())[codes.get(type_id, "unknown")] += 1
    for slot, c in sorted(table.items()):
        print(f"{slot:28} " + ", ".join(f"{k}={v}" for k, v in c.most_common()))
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    import time

    from app.extract.pipeline import extract_page_fields, pages_needing_extraction
    from app.models import DocumentPage

    db = _database()
    missing: Counter = Counter()
    stored = 0
    started = time.perf_counter()
    with db.session_factory() as session:
        ids = pages_needing_extraction(session, force=args.force)
        for n, page_id in enumerate(ids, start=1):
            result = extract_page_fields(session, session.get(DocumentPage, page_id))
            stored += len(result.stored)
            missing.update(result.missing_required)
            if n % 100 == 0 or n == len(ids):
                session.commit()
                print(f"  {n}/{len(ids)} pages", flush=True)
        session.commit()
    db.dispose()
    print(f"extracted: {len(ids)} pages, {stored} fields in {time.perf_counter() - started:.0f}s")
    print(
        "required fields not found: "
        + (", ".join(f"{k}={v}" for k, v in missing.most_common()) or "none")
    )
    return 0


def cmd_extract_eval(args: argparse.Namespace) -> int:
    from app.extract.evaluate import evaluate

    settings = get_settings()
    sets = [
        ("tuning", settings.field_labels_file),
        ("held-out", settings.field_labels_heldout_file),
    ]
    if args.labels:
        sets = [("labels", Path(args.labels))]
    db = _database()
    with db.session_factory() as session:
        for name, path in sets:
            if not path.is_file():
                print(f"{name}: labels not found ({path})")
                continue
            print(f"== {name} set")
            for line in evaluate(session, path).lines():
                print(line)
    db.dispose()
    return 0


def cmd_extraction_report(_: argparse.Namespace) -> int:
    from app.extract.pipeline import extraction_report

    db = _database()
    with db.session_factory() as session:
        report = extraction_report(session)
    db.dispose()
    print("fields found per document type: pages with the field (high confidence)")
    for code, c in sorted(report.items()):
        keys = sorted(k for k in c if k != "pages" and not k.endswith(" high"))
        print(f"{code} ({c['pages']} pages)")
        print("  " + ", ".join(f"{k}={c[k]} ({c[k + ' high']})" for k in keys))
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    from app.models import Submission
    from app.validate.engine import validate_submission

    db = _database()
    verdicts: Counter = Counter()
    codes: Counter = Counter()
    with db.session_factory() as session:
        for sub in session.scalars(select(Submission).order_by(Submission.created_at)):
            result = validate_submission(session, sub)
            verdicts[result.verdict] += 1
            codes.update(result.issues)
        session.commit()
    db.dispose()
    print("submissions: " + ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))
    print("open issues: " + (", ".join(f"{k}={v}" for k, v in codes.most_common()) or "none"))
    return 0


def cmd_validation_report(_: argparse.Namespace) -> int:
    from app.models import ValidationIssue

    db = _database()
    with db.session_factory() as session:
        rows = session.execute(
            select(
                ValidationIssue.severity,
                ValidationIssue.code,
                ValidationIssue.status,
                func.count(func.distinct(ValidationIssue.submission_id)),
                func.count(),
            ).group_by(ValidationIssue.severity, ValidationIssue.code, ValidationIssue.status)
        ).all()
    db.dispose()
    print("severity  issue                                    status     students  issues")
    order = {"error": 0, "warning": 1, "info": 2}
    for sev, code, status, students, n in sorted(rows, key=lambda r: (order[r[0]], -r[4])):
        print(f"{sev:9} {code:40} {status:10} {students:8}  {n:6}")
    return 0


def _read_new_password(email: str) -> str | None:
    import getpass

    from app.auth.passwords import password_problems

    for _ in range(3):
        pw = getpass.getpass("New password (12+ characters): ")
        problems = password_problems(pw, email)
        if problems:
            print("  " + " ".join(problems))
            continue
        if getpass.getpass("Repeat it: ") != pw:
            print("  The two passwords differ.")
            continue
        return pw
    return None


def _temporary_password() -> str:
    import secrets

    return "-".join(secrets.token_urlsafe(6) for _ in range(3))


def cmd_create_user(args: argparse.Namespace) -> int:
    from app.auth.passwords import hash_password
    from app.db.base import utcnow
    from app.models import Institution, User
    from app.models.enums import UserRole
    from app.services.audit import record_event

    role = UserRole(args.role)
    email = args.email.strip().lower()
    db = _database()
    with db.session_factory() as session:
        if session.scalar(select(User).where(User.email == email)):
            print("error: a user with that email already exists", file=sys.stderr)
            return 2
        inst = None
        if role != UserRole.PLATFORM_ADMIN:
            inst = session.scalar(select(Institution).where(Institution.slug == args.institution))
            if inst is None:
                print(f"error: unknown institution {args.institution!r}", file=sys.stderr)
                return 2
        if args.temporary_password:
            password = _temporary_password()
        else:
            password = _read_new_password(email)
            if password is None:
                return 2
        user = User(
            email=email,
            full_name=args.name,
            role=role,
            institution_id=inst.id if inst else None,
            password_hash=hash_password(password),
            password_changed_at=utcnow(),
        )
        session.add(user)
        session.flush()
        record_event(
            session,
            action="user.created",
            entity_type="user",
            entity_id=user.id,
            institution_id=user.institution_id,
            details={"role": role.value},
        )
        session.commit()
    db.dispose()
    print(f"created {role.value} account; the authenticator app is set up at first sign-in")
    if args.temporary_password:
        print(f"temporary password (shown once; change it after signing in): {password}")
    return 0


def _with_user(email: str, action):
    from app.models import User

    db = _database()
    with db.session_factory() as session:
        user = session.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            print("error: no user with that email", file=sys.stderr)
            return 2
        code = action(session, user)
        session.commit()
    db.dispose()
    return code


def cmd_set_password(args: argparse.Namespace) -> int:
    from app.auth.service import set_password

    def action(session, user) -> int:
        password = (
            _temporary_password() if args.temporary_password else _read_new_password(user.email)
        )
        if password is None:
            return 2
        set_password(session, user, password)
        print("password set; the user is signed out everywhere")
        if args.temporary_password:
            print(f"temporary password (shown once): {password}")
        return 0

    return _with_user(args.email, action)


def cmd_reset_2fa(args: argparse.Namespace) -> int:
    from app.auth.service import reset_mfa

    def action(session, user) -> int:
        reset_mfa(session, user)
        print("authenticator removed; a new one is set up at the next sign-in")
        return 0

    return _with_user(args.email, action)


def cmd_set_active(args: argparse.Namespace) -> int:
    from app.auth.service import revoke_all
    from app.services.audit import record_event

    def action(session, user) -> int:
        user.is_active = args.active
        if not args.active:
            revoke_all(session, user)
        user.failed_logins = 0
        user.locked_until = None
        record_event(
            session,
            action="user.enabled" if args.active else "user.disabled",
            entity_type="user",
            entity_id=user.id,
        )
        print("account enabled" if args.active else "account disabled and signed out")
        return 0

    return _with_user(args.email, action)


def cmd_list_users(_: argparse.Namespace) -> int:
    from app.models import Institution, User

    db = _database()
    with db.session_factory() as session:
        rows = session.execute(
            select(User, Institution.slug).outerjoin(Institution).order_by(User.email)
        ).all()
        for u, slug in rows:
            flags = [
                u.role.value,
                slug or "-",
                "active" if u.is_active else "DISABLED",
                "2fa" if u.totp_enabled_at else "2fa-not-set-up",
            ]
            print(f"{u.email:40} " + "  ".join(flags))
        if not rows:
            print("no users")
    db.dispose()
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    from app.intake.pipeline import PROCESSING_VERSION
    from app.jobs.queue import enqueue_document
    from app.models import Document

    db = _database()
    with db.session_factory() as session:
        q = select(Document.id).order_by(Document.created_at)
        if not args.all:
            q = q.where(
                Document.processing_version.is_(None)
                | (Document.processing_version != PROCESSING_VERSION)
            )
        ids = list(session.scalars(q))
        for doc_id in ids:
            enqueue_document(session, doc_id)
        session.commit()
    db.dispose()
    print(f"queued {len(ids)} documents (start a worker with: make worker)")
    return 0


def cmd_queue_status(_: argparse.Namespace) -> int:
    from app.jobs.queue import queue_counts
    from app.models import Job
    from app.models.enums import JobStatus

    db = _database()
    with db.session_factory() as session:
        counts = queue_counts(session)
        failed = session.execute(
            select(Job.last_error, func.count())
            .where(Job.status == JobStatus.FAILED)
            .group_by(Job.last_error)
        ).all()
    db.dispose()
    print("jobs: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    if failed:
        print("failed: " + ", ".join(f"{e}={n}" for e, n in failed))
    return 0


def _storage(settings):
    from app.storage import EncryptedLocalStorage

    return EncryptedLocalStorage(settings.storage_dir, load_storage_key(settings))


def cmd_import_status(_: argparse.Namespace) -> int:
    from app.models import Applicant, Document, ImportFileRef

    db = _database()
    with db.session_factory() as session:
        applicants = session.scalar(select(func.count()).select_from(Applicant))
        documents = session.scalar(select(func.count()).select_from(Document))
        by_status = Counter(
            dict(
                session.execute(
                    select(ImportFileRef.status, func.count()).group_by(ImportFileRef.status)
                ).all()
            )
        )
        errors = Counter(
            session.scalars(
                select(ImportFileRef.last_error).where(ImportFileRef.last_error.is_not(None))
            )
        )
    db.dispose()
    print(f"applicants: {applicants} | documents: {documents}")
    print("file refs: " + ", ".join(f"{s.value}={n}" for s, n in sorted(by_status.items())))
    for msg, n in errors.most_common(10):
        print(f"  {n} x {msg}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Insert/update the document-type registry").set_defaults(
        func=cmd_seed
    )
    sub.add_parser("generate-key", help="Print a new CERTVERIFY_STORAGE_KEY value").set_defaults(
        func=cmd_generate_key
    )

    p = sub.add_parser("import-form", help="Import the C-DAC Google Form CSV export")
    p.add_argument("csv", help="Path to the CSV file")
    p.set_defaults(func=cmd_import_form)

    sub.add_parser("drive-auth", help="Sign in to Google Drive (opens a browser)").set_defaults(
        func=cmd_drive_auth
    )

    p = sub.add_parser("drive-download", help="Download pending Drive files")
    p.add_argument("--limit", type=int, default=None, help="Only fetch this many files")
    p.add_argument("--retry-failed", action="store_true", help="Also retry failed files")
    p.set_defaults(func=cmd_drive_download)

    p = sub.add_parser(
        "import-folder", help="Import files from a local copy of the form's Drive folder"
    )
    p.add_argument("folder", help="The '... (File responses)' folder")
    p.add_argument("--apply", action="store_true", help="Import (default: dry run + mapping)")
    p.add_argument("--mapping", default=None, help="Mapping CSV path (default: data dir)")
    p.set_defaults(func=cmd_import_folder)

    p = sub.add_parser("process", help="Render, clean and quality-check documents")
    p.add_argument("--limit", type=int, default=None, help="Only process this many documents")
    p.add_argument("--force", action="store_true", help="Re-process even if up to date")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("ocr", help="Read text from processed pages")
    p.add_argument("--limit", type=int, default=None, help="Only OCR this many pages")
    p.add_argument("--force", action="store_true", help="Re-run OCR even if up to date")
    p.add_argument(
        "--engine", choices=["tesseract", "paddle"], default="tesseract", help="OCR engine"
    )
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("ocr-eval", help="Measure OCR against the labelled ground truth")
    p.add_argument("--ground-truth", default=None, help="Ground-truth JSON (default: data dir)")
    p.add_argument(
        "--engine", choices=["tesseract", "paddle"], default="tesseract", help="OCR engine"
    )
    p.set_defaults(func=cmd_ocr_eval)

    p = sub.add_parser("extract", help="Extract fields (names, IDs, marks) from classified pages")
    p.add_argument("--force", action="store_true", help="Re-extract even if up to date")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("extract-eval", help="Measure extraction against labelled pages")
    p.add_argument("--labels", default=None, help="Labels JSON (default: tuning + held-out)")
    p.set_defaults(func=cmd_extract_eval)

    p = sub.add_parser("extraction-report", help="Fields found per document type (counts)")
    p.set_defaults(func=cmd_extraction_report)

    p = sub.add_parser("validate", help="Run validation rules on every submission")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("validation-report", help="Open issues by type and severity (counts)")
    p.set_defaults(func=cmd_validation_report)

    p = sub.add_parser("create-user", help="Create a staff account")
    p.add_argument("email")
    p.add_argument("--name", required=True)
    p.add_argument(
        "--role", required=True, choices=["verifier", "institution_admin", "platform_admin"]
    )
    p.add_argument("--institution", default="cdac-kharghar", help="Institution slug")
    p.add_argument(
        "--temporary-password",
        action="store_true",
        help="Generate a password and print it once instead of asking",
    )
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("set-password", help="Set a user's password (signs them out)")
    p.add_argument("email")
    p.add_argument("--temporary-password", action="store_true")
    p.set_defaults(func=cmd_set_password)

    p = sub.add_parser("reset-2fa", help="Remove a user's authenticator (lost phone)")
    p.add_argument("email")
    p.set_defaults(func=cmd_reset_2fa)

    p = sub.add_parser("disable-user", help="Disable an account and sign it out")
    p.add_argument("email")
    p.set_defaults(func=cmd_set_active, active=False)

    p = sub.add_parser("enable-user", help="Re-enable an account (also clears a lockout)")
    p.add_argument("email")
    p.set_defaults(func=cmd_set_active, active=True)

    p = sub.add_parser("list-users", help="List accounts (no secrets)")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("enqueue", help="Queue documents for background processing")
    p.add_argument("--all", action="store_true", help="Every document, not only out-of-date ones")
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("queue-status", help="Background job counts")
    p.set_defaults(func=cmd_queue_status)

    p = sub.add_parser("classify", help="Classify OCR'd pages (type, semester, level)")
    p.add_argument("--force", action="store_true", help="Re-classify even if up to date")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("classify-eval", help="Score classification against labelled pages")
    p.add_argument("--labels", default=None, help="Labels JSON (default: data dir)")
    p.set_defaults(func=cmd_classify_eval)

    sub.add_parser(
        "classification-report", help="Detected page types per upload slot"
    ).set_defaults(func=cmd_classification_report)

    sub.add_parser("quality-report", help="Summarise page quality verdicts").set_defaults(
        func=cmd_quality_report
    )

    sub.add_parser("import-status", help="Show import/download progress").set_defaults(
        func=cmd_import_status
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
