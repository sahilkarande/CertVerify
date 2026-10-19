"""Management commands: `python -m app.cli <command>`."""

import argparse
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import generate_key
from app.db.session import Database
from app.seeds.document_types import seed_document_types


def cmd_seed(_: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings.database_url)
    with db.session_factory() as session, session.begin():
        created, updated = seed_document_types(session)
    db.dispose()
    print(f"document types: {created} created, {updated} updated")
    return 0


def cmd_generate_key(_: argparse.Namespace) -> int:
    print(generate_key())
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
