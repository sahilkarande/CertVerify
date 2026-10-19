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
        f"{report.files_already_present} | Drive links marked fetched: {report.drive_refs_linked}"
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


def cmd_ocr(args: argparse.Namespace) -> int:
    import time

    from app.models import DocumentPage
    from app.ocr.engine import TesseractEngine
    from app.ocr.pipeline import ocr_page, pages_needing_ocr

    settings = get_settings()
    storage = _storage(settings)
    engine = TesseractEngine()
    db = _database()
    statuses: Counter = Counter()
    rotated = 0
    started = time.perf_counter()
    with db.session_factory() as session:
        ids = pages_needing_ocr(session, engine.name, force=args.force)
        if args.limit:
            ids = ids[: args.limit]
        for n, page_id in enumerate(ids, start=1):
            row = ocr_page(session, storage, engine, session.get(DocumentPage, page_id))
            session.commit()  # per page: an interrupted run resumes where it stopped
            statuses[row.status.value] += 1
            rotated += row.rotation != 0
            if n % 25 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} pages", flush=True)
    db.dispose()
    secs = time.perf_counter() - started
    print(
        f"OCR: {sum(statuses.values())} pages in {secs:.0f}s "
        f"({secs / max(sum(statuses.values()), 1):.1f}s/page) | rotated: {rotated}"
    )
    print("status: " + (", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) or "none"))
    return 0


def cmd_ocr_eval(args: argparse.Namespace) -> int:
    from app.ocr.engine import TesseractEngine
    from app.ocr.evaluate import evaluate

    settings = get_settings()
    gt = Path(args.ground_truth) if args.ground_truth else settings.ocr_ground_truth_file
    if not gt.is_file():
        print(f"error: ground truth not found: {gt}", file=sys.stderr)
        return 2
    db = _database()
    with db.session_factory() as session:
        report = evaluate(session, _storage(settings), TesseractEngine(), gt)
    db.dispose()
    for line in report.lines():
        print(line)
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
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("ocr-eval", help="Measure OCR against the labelled ground truth")
    p.add_argument("--ground-truth", default=None, help="Ground-truth JSON (default: data dir)")
    p.set_defaults(func=cmd_ocr_eval)

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
