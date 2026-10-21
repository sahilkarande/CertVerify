import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    RequirementItem,
    ValidationIssue,
)
from app.models.enums import (
    DocumentStatus,
    IssueSeverity,
    IssueStatus,
    QualityVerdict,
    SubmissionStatus,
)
from app.seeds.document_types import seed_document_types
from app.validate.context import DocInfo, FieldValue, PageInfo, SubmissionContext
from app.validate.engine import validate_submission, verdict
from app.validate.rules import (
    accepted_types,
    claimed_percentage,
    completeness,
    duplicate_uploads,
    marks_consistent,
    name_matches_form,
    semester_coverage,
    shared_identifiers,
    unreadable,
    wrong_slot,
)
from tests.factories import make_stored_file, make_submission

SEM, PROV, DEG = "semester_marksheet", "provisional_degree_certificate", "degree_certificate"
DEGREE_SLOT = {
    "attribute": "degree_status",
    "accepted_by_value": {"has_degree": [DEG], "has_provisional": [PROV, DEG]},
}


def item(key, types, condition=None, label=None) -> RequirementItem:
    return RequirementItem(
        key=key,
        label=label or key,
        accepted_type_codes=types,
        is_required=True,
        condition=condition,
    )


def page(type_code=None, sems=(), quality=QualityVerdict.OK, **fields) -> PageInfo:
    return PageInfo(
        uuid.uuid4(),
        1,
        type_code,
        list(sems),
        quality,
        {
            k: FieldValue(*v) if isinstance(v, tuple) else FieldValue(v, 0.95)
            for k, v in fields.items()
        },
    )


def doc(slot, *pages, sha=None, status=DocumentStatus.PROCESSED) -> DocInfo:
    for n, p in enumerate(pages, start=1):
        p.number = n
    return DocInfo(uuid.uuid4(), slot, sha or uuid.uuid4().hex, status, list(pages))


def ctx(*docs, name="Asha Ramesh Patil", requirements=(), **attrs) -> SubmissionContext:
    return SubmissionContext(uuid.uuid4(), name, attrs, list(docs), list(requirements))


def codes(issues) -> list[tuple[str, IssueSeverity]]:
    return sorted((i.code, i.severity) for i in issues)


# ---------- completeness ----------


def test_accepted_types_depend_on_the_form_answer() -> None:
    it = item("degree", [DEG, PROV, SEM], DEGREE_SLOT)
    assert accepted_types(it, {"degree_status": "has_degree"}) == [DEG]
    assert accepted_types(it, {"degree_status": "awaiting_provisional"}) == [DEG, PROV, SEM]


def test_present_anywhere_counts_even_in_the_wrong_slot() -> None:
    c = ctx(
        doc("ssc", page("hsc_marksheet")),
        doc("hsc", page("ssc_marksheet")),
        requirements=[item("ssc", ["ssc_marksheet"]), item("hsc", ["hsc_marksheet"])],
    )
    assert completeness(c) == []
    assert codes(wrong_slot(c)) == [("wrong_slot", IssueSeverity.INFO)] * 2


def test_missing_substituted_and_unrecognised_documents() -> None:
    reqs = [item("degree", [DEG, PROV, SEM], DEGREE_SLOT), item("photo", ["passport_photo"])]
    missing = ctx(
        doc("degree", page("ssc_marksheet")), requirements=reqs, degree_status="has_degree"
    )
    assert codes(completeness(missing)) == [("missing_document", IssueSeverity.ERROR)] * 2
    substituted = ctx(
        doc("degree", page(PROV)), doc("photo"), requirements=reqs, degree_status="has_degree"
    )
    [issue] = completeness(substituted)
    assert (issue.code, issue.details["found"]) == ("expected_document_substituted", [PROV])
    unknown = ctx(
        doc("degree", page(None)), doc("photo"), requirements=reqs, degree_status="has_degree"
    )
    assert codes(completeness(unknown)) == [("unconfirmed_document", IssueSeverity.WARNING)]
    # a provisional certificate in another slot is not a substitute for this one
    elsewhere = ctx(
        doc("degree", page("ssc_marksheet")),
        doc("other", page(PROV)),
        doc("photo"),
        requirements=reqs,
        degree_status="has_degree",
    )
    assert codes(completeness(elsewhere)) == [("missing_document", IssueSeverity.ERROR)]


def test_degree_satisfies_a_provisional_requirement() -> None:
    c = ctx(
        doc("degree", page(DEG)),
        requirements=[item("degree", [PROV], DEGREE_SLOT)],
        degree_status="has_provisional",
    )
    assert completeness(c) == []


# ---------- semesters ----------


def test_semester_coverage_counts_pages_and_year_statements() -> None:
    full = ctx(doc("sem", page(SEM, [1, 2]), page(SEM, [3]), page(SEM), page(SEM, [5, 6])))
    assert semester_coverage(full) == []  # 2 + 1 + 1 (unnumbered) + 2 = 6
    short = ctx(doc("sem", page(SEM, [1]), page(SEM, [7, 8])))
    [issue] = semester_coverage(short)
    assert issue.details["covered"] == 3 and issue.details["numbers_not_seen"] == [2, 3, 4, 5, 6]


def test_lateral_entry_diploma_holders_start_at_semester_3() -> None:
    c = ctx(
        doc("hsc", page("diploma_marksheet")),
        doc("sem", page(SEM, [3, 4]), page(SEM, [5, 6]), page(SEM, [7, 8])),
    )
    assert semester_coverage(c) == []
    without_diploma = ctx(doc("sem", page(SEM, [3, 4]), page(SEM, [5, 6]), page(SEM, [7, 8])))
    assert codes(semester_coverage(without_diploma)) == [
        ("semester_marksheets_missing", IssueSeverity.WARNING)
    ]


# ---------- duplicates ----------


def test_duplicate_uploads_expected_or_not() -> None:
    allowed = ctx(
        doc("provisional_or_latest_sem", page(PROV), sha="a" * 64),
        doc("degree_or_fallback", page(PROV), sha="a" * 64),
    )
    assert codes(duplicate_uploads(allowed)) == [("duplicate_upload", IssueSeverity.INFO)]
    odd = ctx(
        doc("ssc", page("ssc_marksheet"), sha="b" * 64),
        doc("hsc_or_diploma", page("ssc_marksheet"), sha="b" * 64),
    )
    assert codes(duplicate_uploads(odd)) == [("duplicate_upload", IssueSeverity.WARNING)]
    awaiting = ctx(
        doc("semester_marksheets", page(SEM), sha="c" * 64),
        doc("provisional_or_latest_sem", page(SEM), sha="c" * 64),
        degree_status="awaiting_provisional",
    )
    assert codes(duplicate_uploads(awaiting)) == [("duplicate_upload", IssueSeverity.INFO)]


def test_files_and_ids_shared_with_other_applicants_are_errors() -> None:
    c = ctx(doc("ssc", page("ssc_marksheet", seat_number="B190244471"), sha="d" * 64))
    c.shared_hashes = {"d" * 64: 1}
    c.shared_ids = {"B190244471": 2}
    assert codes(duplicate_uploads(c)) == [
        ("file_shared_with_other_applicant", IssueSeverity.ERROR)
    ]
    [issue] = shared_identifiers(c)
    assert issue.severity is IssueSeverity.ERROR and issue.details["other_applicants"] == 2


# ---------- identity ----------


def test_name_judged_per_document_and_middle_name_optional() -> None:
    ok = ctx(
        doc(
            "sem",
            page(SEM, candidate_name="SURNAME FIRST"),
            page(SEM, candidate_name="PATIL ASHA RAMESH"),
        )
    )
    assert name_matches_form(ok) == []  # one page with the full name is enough
    two_words = ctx(
        doc("sem", page(SEM, candidate_name="VERMA RAHUL"), page(SEM, candidate_name="PATIL ASHA")),
        name="Asha Patil",
    )
    assert name_matches_form(two_words) == []
    no_middle = ctx(doc("ssc", page("ssc_marksheet", candidate_name="ASHA PATIL")))
    assert name_matches_form(no_middle) == []
    changed = ctx(doc("ssc", page("ssc_marksheet", candidate_name="ASHA SURESH KULKARNI")))
    assert codes(name_matches_form(changed)) == [("name_partial_match", IssueSeverity.WARNING)]
    other = ctx(doc("ssc", page("ssc_marksheet", candidate_name="RAHUL VERMA")))
    [issue] = name_matches_form(other)
    assert issue.severity is IssueSeverity.ERROR and issue.details["printed_name"] == "RAHUL VERMA"


# ---------- marks ----------


def test_marks_check_uses_confident_values_only() -> None:
    bad = ctx(
        doc(
            "ssc",
            page(
                "ssc_marksheet", total_marks_obtained="400", total_marks_max="500", percentage="90"
            ),
        )
    )
    assert codes(marks_consistent(bad)) == [("marks_inconsistent", IssueSeverity.WARNING)]
    unsure = ctx(
        doc(
            "ssc",
            page(
                "ssc_marksheet",
                total_marks_obtained=("400", 0.6),
                total_marks_max=("500", 0.6),
                percentage=("90", 0.6),
            ),
        )
    )
    assert marks_consistent(unsure) == []


@pytest.mark.parametrize(
    ("claimed", "expected"),
    [(80.0, []), (75.5, []), (87.0, [("claimed_percentage_mismatch", IssueSeverity.WARNING)])],
)
def test_claimed_percentage_against_cgpa_conversions(claimed, expected) -> None:
    c = ctx(doc("degree", page(DEG, cgpa="8.0")), claimed_aggregate_percent=claimed)
    assert codes(claimed_percentage(c)) == expected


def test_claimed_percentage_ignores_an_earlier_semesters_cgpa() -> None:
    c = ctx(
        doc("sem", page(SEM, [4], cgpa="6.0"), page(SEM, [7, 8])), claimed_aggregate_percent=85.0
    )
    assert codes(claimed_percentage(c)) == [("claimed_percentage_unverified", IssueSeverity.INFO)]


def test_unreadable_documents_and_pages() -> None:
    c = ctx(
        doc("ssc", status=DocumentStatus.FAILED),
        doc("sem", page(SEM), page(SEM, quality=QualityVerdict.RETAKE)),
        doc("photo", page(quality=QualityVerdict.RETAKE)),
    )
    assert codes(unreadable(c)) == [
        ("document_unreadable", IssueSeverity.ERROR),
        ("page_quality_poor", IssueSeverity.WARNING),
    ]


def test_verdict() -> None:
    assert verdict([]) == "clear"
    assert verdict([IssueSeverity.INFO, IssueSeverity.WARNING]) == "review"
    assert verdict([IssueSeverity.WARNING, IssueSeverity.ERROR]) == "problems"


# ---------- engine with database ----------


@pytest.fixture
def submission(session: Session):
    seed_document_types(session)
    sub = make_submission(session)
    sub.applicant.full_name = "Asha Ramesh Patil"
    sub.status = SubmissionStatus.SUBMITTED
    ssc = session.scalar(select(DocumentType).where(DocumentType.code == "ssc_marksheet"))
    d = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="ssc")
    session.add(d)
    session.flush()
    p = DocumentPage(document_id=d.id, page_number=1, detected_type_id=ssc.id)
    session.add(p)
    session.flush()
    session.add(
        ExtractedField(
            document_id=d.id,
            page_id=p.id,
            field_key="candidate_name",
            machine_value="RAHUL VERMA",
            value="RAHUL VERMA",
            confidence=0.6,
        )
    )
    session.commit()
    return sub


def _issues(db: Session, sub) -> list[ValidationIssue]:
    return db.scalars(select(ValidationIssue).where(ValidationIssue.submission_id == sub.id)).all()


def test_engine_stores_issues_and_routes_to_review(session: Session, submission) -> None:
    result = validate_submission(session, submission)
    session.commit()
    [issue] = [i for i in _issues(session, submission) if i.code == "name_mismatch"]
    assert result.verdict == "problems" and result.issues["name_mismatch"] == 1
    assert issue.severity is IssueSeverity.ERROR and issue.status is IssueStatus.OPEN
    assert issue.details["rules_version"] == 1 and issue.document_id is not None
    assert submission.status is SubmissionStatus.NEEDS_REVIEW

    validate_submission(session, submission)  # re-run replaces, never duplicates
    session.commit()
    assert len(_issues(session, submission)) == len(result.issues)


def test_dismissed_issue_stays_dismissed(session: Session, submission) -> None:
    validate_submission(session, submission)
    session.commit()
    issue = next(i for i in _issues(session, submission) if i.code == "name_mismatch")
    issue.status = IssueStatus.DISMISSED  # a verifier checked: name change, documented
    session.commit()

    result = validate_submission(session, submission)
    session.commit()
    mismatches = [i for i in _issues(session, submission) if i.code == "name_mismatch"]
    assert [i.status for i in mismatches] == [IssueStatus.DISMISSED]
    assert "name_mismatch" not in result.issues
