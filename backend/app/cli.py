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

    sub.add_parser("import-status", help="Show import/download progress").set_defaults(
        func=cmd_import_status
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
