"""Import the C-DAC form's uploaded files from a local copy of the Drive "File responses" folder.

The export has one sub-folder per upload question and names each file
`<original name> - <respondent's Google account name>.<ext>`. Drive IDs are lost, so files are
linked to form rows by name. The process has two steps:

1. `plan_mapping` matches respondent names to submissions and writes a mapping CSV. Uncertain
   matches are left unconfirmed for a person to check.
2. `apply_import` imports files only once every mapping row is confirmed.
"""

import csv
import difflib
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.importers.cdac_form import schema
from app.importers.cdac_form.importer import DRIVE_SOURCE, ensure_requirement_set
from app.models import Applicant, Document, ImportFileRef, StoredFile, Submission
from app.models.enums import ImportFileStatus
from app.services.audit import record_event
from app.services.files import store_file
from app.services.filetypes import sniff_mime
from app.services.names import normalize_name
from app.storage import ObjectStorage

MAPPING_COLUMNS = ["respondent_name", "form_number", "form_name", "method", "confirmed"]
# Methods considered safe enough to confirm automatically.
AUTO_CONFIRMED = {"exact", "all_words", "similar_and_email"}
SIMILAR_MIN = 0.75
SIMILAR_MARGIN = 0.10
EMAIL_MIN = 0.80
MAX_FILE_BYTES = 25 * 1024 * 1024


class FolderImportError(ValueError):
    pass


@dataclass
class ScannedFolder:
    # normalised respondent name -> slot key -> files
    files: dict[str, dict[str, list[Path]]]
    display_names: dict[str, str]  # normalised -> name as written in the file name
    warnings: list[str] = field(default_factory=list)


@dataclass
class MappingRow:
    respondent_name: str
    form_number: str
    form_name: str
    method: str
    confirmed: bool


@dataclass
class ApplyReport:
    students: int = 0
    files_stored: int = 0
    files_already_present: int = 0
    drive_refs_linked: int = 0
    by_mime: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    warnings: list[str] = field(default_factory=list)


def respondent_from_filename(name: str) -> str | None:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if " - " not in stem:
        return None
    respondent = stem.rsplit(" - ", 1)[1]
    return re.sub(r"\s*\(\d+\)$", "", respondent).strip() or None  # drop " (1)" copy markers


def scan_folder(root: Path) -> ScannedFolder:
    if not root.is_dir():
        raise FolderImportError(f"Not a directory: {root}")

    slot_dirs: dict[str, Path] = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        slot = next((s for s in schema.UPLOAD_SLOTS if d.name.startswith(s.header_prefix)), None)
        if slot is None:
            raise FolderImportError(f"Folder does not match any upload question: {d.name!r}")
        if slot.key in slot_dirs:
            raise FolderImportError(f"Two folders for upload question {slot.key!r}")
        slot_dirs[slot.key] = d
    missing = [s.key for s in schema.UPLOAD_SLOTS if s.key not in slot_dirs]
    if missing:
        raise FolderImportError(f"Missing folders for upload questions: {missing}")

    files: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    display: dict[str, str] = {}
    warnings: list[str] = []
    for slot_key, d in slot_dirs.items():
        for f in sorted(p for p in d.iterdir() if p.is_file()):
            respondent = respondent_from_filename(f.name)
            if respondent is None:
                warnings.append(f"slot '{slot_key}': a file has no ' - <name>' suffix; skipped")
                continue
            key = normalize_name(respondent)
            files[key][slot_key].append(f)
            display.setdefault(key, respondent)

    for key, slots in files.items():
        absent = [s.key for s in schema.UPLOAD_SLOTS if s.key not in slots]
        if absent:
            warnings.append(f"respondent #{_short(key)}: no file in slots {absent}")
        many = [k for k, v in slots.items() if len(v) > 1]
        if many:
            warnings.append(f"respondent #{_short(key)}: several files in slots {many}")
    return ScannedFolder(
        files={k: dict(v) for k, v in files.items()}, display_names=display, warnings=warnings
    )


def _short(normalised_name: str) -> str:
    """Stable non-identifying tag for warnings (no names in logs)."""
    return hashlib.sha256(normalised_name.encode()).hexdigest()[:8]


def _email_similarity(respondent: str, email: str | None) -> float:
    if not email:
        return 0.0
    local = re.sub(r"[^a-z]", "", email.split("@")[0].lower())
    compact = respondent.replace(" ", "")
    return difflib.SequenceMatcher(None, compact, local).ratio() if local else 0.0


def plan_mapping(db: Session, scanned: ScannedFolder) -> list[MappingRow]:
    rs = ensure_requirement_set(db)
    candidates = {
        a.external_ref: a
        for a in db.scalars(select(Applicant).where(Applicant.institution_id == rs.institution_id))
    }
    if not candidates:
        raise FolderImportError("No applicants found. Run import-form first.")
    norm = {ref: normalize_name(a.full_name) for ref, a in candidates.items()}

    proposals: dict[str, tuple[str, str]] = {}  # respondent -> (form number, method)
    for resp in scanned.files:
        tokens = set(resp.split())
        exact = [r for r, n in norm.items() if n == resp]
        if len(exact) == 1:
            proposals[resp] = (exact[0], "exact")
            continue
        contained = [r for r, n in norm.items() if tokens and tokens <= set(n.split())]
        if len(contained) == 1:
            proposals[resp] = (contained[0], "all_words")
            continue
        scored = sorted(
            ((difflib.SequenceMatcher(None, resp, n).ratio(), r) for r, n in norm.items()),
            reverse=True,
        )
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, ""))
        if best[0] >= SIMILAR_MIN and best[0] - second[0] >= SIMILAR_MARGIN:
            email_ok = _email_similarity(resp, candidates[best[1]].email) >= EMAIL_MIN
            proposals[resp] = (best[1], "similar_and_email" if email_ok else "similar")

    # A form row claimed by two respondents is ambiguous for both.
    claims: dict[str, list[str]] = defaultdict(list)
    for resp, (ref, _) in proposals.items():
        claims[ref].append(resp)
    for ref, resps in claims.items():
        if len(resps) > 1:
            for resp in resps:
                proposals[resp] = (ref, "conflict")

    # Last resort: if exactly one respondent and one form row remain, pair them by elimination.
    unmatched = [r for r in scanned.files if r not in proposals]
    free = [ref for ref in candidates if ref not in claims]
    if len(unmatched) == 1 and len(free) == 1:
        proposals[unmatched[0]] = (free[0], "elimination")

    rows = []
    for resp in sorted(scanned.files, key=lambda r: scanned.display_names[r].lower()):
        ref, method = proposals.get(resp, ("", "unmatched"))
        rows.append(
            MappingRow(
                respondent_name=scanned.display_names[resp],
                form_number=ref,
                form_name=candidates[ref].full_name if ref else "",
                method=method,
                confirmed=method in AUTO_CONFIRMED,
            )
        )
    return rows


def write_mapping(path: Path, rows: list[MappingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(MAPPING_COLUMNS)
        for r in rows:
            confirmed = "yes" if r.confirmed else "no"
            w.writerow([r.respondent_name, r.form_number, r.form_name, r.method, confirmed])


def read_mapping(path: Path) -> list[MappingRow]:
    if not path.is_file():
        raise FolderImportError(f"Mapping file not found: {path}. Run the dry run first.")
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != MAPPING_COLUMNS:
            raise FolderImportError(f"Mapping file must have columns {MAPPING_COLUMNS}")
        return [
            MappingRow(
                respondent_name=r["respondent_name"],
                form_number=r["form_number"].strip(),
                form_name=r["form_name"],
                method=r["method"],
                confirmed=r["confirmed"].strip().lower() in {"yes", "y", "true", "1"},
            )
            for r in reader
        ]


def _validate_mapping(scanned: ScannedFolder, rows: list[MappingRow]) -> dict[str, str]:
    """Return respondent (normalised) -> form number, or raise listing every problem."""
    problems: list[str] = []
    mapping: dict[str, str] = {}
    for i, r in enumerate(rows, start=2):
        key = normalize_name(r.respondent_name)
        if key not in scanned.files:
            problems.append(f"line {i}: respondent not found in the folder")
        elif not r.confirmed:
            problems.append(f"line {i}: not confirmed (method: {r.method})")
        elif not r.form_number:
            problems.append(f"line {i}: confirmed but form_number is empty")
        else:
            mapping[key] = r.form_number
    for key in scanned.files:
        if key not in {normalize_name(r.respondent_name) for r in rows}:
            problems.append(f"respondent #{_short(key)} is in the folder but not in the mapping")
    seen: dict[str, int] = defaultdict(int)
    for ref in mapping.values():
        seen[ref] += 1
    problems += [f"form number {ref} is used {n} times" for ref, n in seen.items() if n > 1]
    if problems:
        raise FolderImportError("Mapping is not ready:\n  - " + "\n  - ".join(problems))
    return mapping


def apply_import(
    db: Session,
    storage: ObjectStorage,
    scanned: ScannedFolder,
    rows: list[MappingRow],
) -> ApplyReport:
    """Import files student by student; each student is its own transaction."""
    mapping = _validate_mapping(scanned, rows)
    rs = ensure_requirement_set(db)
    db.commit()

    report = ApplyReport(warnings=list(scanned.warnings))
    for resp, form_number in mapping.items():
        submission = db.scalar(
            select(Submission)
            .join(Applicant)
            .where(
                Applicant.institution_id == rs.institution_id,
                Applicant.external_ref == form_number,
                Submission.requirement_set_id == rs.id,
            )
        )
        if submission is None:
            raise FolderImportError(f"No submission for form number {form_number}")

        written_keys: list[str] = []
        try:
            for slot_key, paths in scanned.files[resp].items():
                existing_hashes = set(
                    db.scalars(
                        select(StoredFile.sha256)
                        .join(Document, Document.file_id == StoredFile.id)
                        .where(
                            Document.submission_id == submission.id, Document.slot_key == slot_key
                        )
                    )
                )
                new_docs: list[Document] = []
                for path in paths:
                    data = path.read_bytes()
                    if len(data) > MAX_FILE_BYTES:
                        report.warnings.append(
                            f"form {form_number}, slot '{slot_key}': file over "
                            f"{MAX_FILE_BYTES} bytes skipped"
                        )
                        continue
                    sha = hashlib.sha256(data).hexdigest()
                    if sha in existing_hashes:  # already imported in an earlier run
                        report.files_already_present += 1
                        continue
                    stored = store_file(
                        db,
                        storage,
                        data,
                        mime_type=sniff_mime(data),
                        original_filename=path.name,
                        institution_id=rs.institution_id,
                    )
                    written_keys.append(stored.object_key)
                    existing_hashes.add(sha)
                    doc = Document(
                        submission_id=submission.id,
                        file_id=stored.id,
                        slot_key=slot_key,
                        source_ref=f"folder:{path.name}"[:255],
                    )
                    db.add(doc)
                    new_docs.append(doc)
                    report.files_stored += 1
                    report.by_mime[stored.mime_type] += 1
                db.flush()
                report.drive_refs_linked += _link_drive_ref(db, submission.id, slot_key, new_docs)
            db.commit()
        except Exception:
            db.rollback()
            for key in written_keys:
                storage.delete(key)
            raise
        report.students += 1

    record_event(
        db,
        action="import.folder",
        entity_type="requirement_set",
        entity_id=rs.id,
        institution_id=rs.institution_id,
        details={
            "students": report.students,
            "files_stored": report.files_stored,
            "files_already_present": report.files_already_present,
            "drive_refs_linked": report.drive_refs_linked,
        },
    )
    db.commit()
    return report


def _link_drive_ref(db: Session, submission_id, slot_key: str, new_docs: list[Document]) -> int:
    """If the slot has exactly one pending Drive link and exactly one new file, they are the same
    upload: mark the link as fetched so the Drive downloader will not fetch it again."""
    if len(new_docs) != 1:
        return 0
    refs = list(
        db.scalars(
            select(ImportFileRef).where(
                ImportFileRef.submission_id == submission_id,
                ImportFileRef.slot_key == slot_key,
                ImportFileRef.source == DRIVE_SOURCE,
            )
        )
    )
    if len(refs) != 1 or refs[0].status == ImportFileStatus.FETCHED:
        return 0
    ref = refs[0]
    ref.status = ImportFileStatus.FETCHED
    ref.document_id = new_docs[0].id
    ref.last_error = None
    ref.fetched_at = utcnow()
    return 1
