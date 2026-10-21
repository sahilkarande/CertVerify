"""Write parsed form rows into the database. Safe to run repeatedly: applicants and
submissions are updated in place and Drive file refs are only added once."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.cdac_form import schema
from app.importers.cdac_form.parser import FormRow, ParseResult
from app.models import (
    Applicant,
    ImportFileRef,
    Institution,
    RequirementItem,
    RequirementSet,
    Submission,
)
from app.models.enums import ApplicantSource, SubmissionStatus
from app.services.audit import record_event

DRIVE_SOURCE = "gdrive"


@dataclass
class ImportReport:
    rows: int = 0
    applicants_created: int = 0
    applicants_updated: int = 0
    file_refs_created: int = 0
    file_refs_existing: int = 0
    warnings: list[str] = field(default_factory=list)


def ensure_requirement_set(db: Session) -> RequirementSet:
    """Create or update the C-DAC institution and its requirement set."""
    inst = db.scalar(select(Institution).where(Institution.slug == schema.INSTITUTION_SLUG))
    if inst is None:
        inst = Institution(slug=schema.INSTITUTION_SLUG, name=schema.INSTITUTION_NAME)
        db.add(inst)
        db.flush()

    rs = db.scalar(
        select(RequirementSet).where(
            RequirementSet.institution_id == inst.id,
            RequirementSet.code == schema.REQUIREMENT_SET_CODE,
        )
    )
    if rs is None:
        rs = RequirementSet(
            institution_id=inst.id,
            code=schema.REQUIREMENT_SET_CODE,
            name=schema.REQUIREMENT_SET_NAME,
        )
        db.add(rs)
        db.flush()

    items = {i.key: i for i in rs.items}
    for position, slot in enumerate(schema.UPLOAD_SLOTS):
        item = items.get(slot.key)
        if item is None:
            item = RequirementItem(key=slot.key)
            rs.items.append(item)
        item.label = slot.label
        item.position = position
        item.accepted_type_codes = list(slot.accepted_type_codes)
        item.condition = slot.condition
        item.is_required = True
        item.min_count = 1
    db.flush()
    return rs


def _attributes(row: FormRow) -> dict:
    return {
        "course": row.course,
        "degree_status": row.degree_status,
        "degree_option_raw": row.degree_option_raw,
        "claimed_aggregate_percent": row.claimed_aggregate_percent,
        "hostel_opted": row.hostel_opted,
        "guardian_phone": row.guardian_phone,
    }


def import_form(db: Session, parsed: ParseResult) -> ImportReport:
    """Import all rows in one transaction (the caller commits)."""
    report = ImportReport(rows=len(parsed.rows), warnings=list(parsed.warnings))
    rs = ensure_requirement_set(db)
    institution_id = rs.institution_id

    existing_refs = {
        r.external_id: r
        for r in db.scalars(select(ImportFileRef).where(ImportFileRef.source == DRIVE_SOURCE))
    }

    for row in parsed.rows:
        applicant = db.scalar(
            select(Applicant).where(
                Applicant.institution_id == institution_id,
                Applicant.external_ref == row.form_number,
            )
        )
        if applicant is None:
            applicant = Applicant(
                institution_id=institution_id,
                external_ref=row.form_number,
                source=ApplicantSource.FORM_IMPORT,
                full_name=row.full_name,
            )
            db.add(applicant)
            report.applicants_created += 1
        else:
            report.applicants_updated += 1
        applicant.full_name = row.full_name
        applicant.email = row.email
        applicant.phone = row.phone
        db.flush()

        submission = db.scalar(
            select(Submission).where(
                Submission.applicant_id == applicant.id,
                Submission.requirement_set_id == rs.id,
            )
        )
        if submission is None:
            # A form response is already a completed submission.
            submission = Submission(
                applicant_id=applicant.id,
                requirement_set_id=rs.id,
                status=SubmissionStatus.SUBMITTED,
            )
            db.add(submission)
        elif submission.status == SubmissionStatus.DRAFT:
            submission.status = SubmissionStatus.SUBMITTED
        submission.submitted_at = row.submitted_at
        submission.attributes = _attributes(row)
        db.flush()

        for slot_key, file_ids in row.drive_files.items():
            for file_id in file_ids:
                ref = existing_refs.get(file_id)
                if ref is not None:
                    report.file_refs_existing += 1
                    if ref.submission_id != submission.id or ref.slot_key != slot_key:
                        report.warnings.append(
                            f"row {row.row_number} (form {row.form_number}): Drive file in slot "
                            f"'{slot_key}' is already linked to another submission/slot"
                        )
                    continue
                ref = ImportFileRef(
                    submission_id=submission.id,
                    slot_key=slot_key,
                    source=DRIVE_SOURCE,
                    external_id=file_id,
                )
                db.add(ref)
                existing_refs[file_id] = ref
                report.file_refs_created += 1
    db.flush()

    record_event(
        db,
        action="import.form",
        entity_type="requirement_set",
        entity_id=rs.id,
        institution_id=institution_id,
        details={
            "rows": report.rows,
            "applicants_created": report.applicants_created,
            "applicants_updated": report.applicants_updated,
            "file_refs_created": report.file_refs_created,
            "warnings": len(report.warnings),
        },
    )
    return report
