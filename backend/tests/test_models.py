import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Applicant,
    Document,
    DocumentPage,
    ExtractedField,
    RequirementSet,
    StoredFile,
    Submission,
    User,
    ValidationIssue,
)
from app.models.enums import (
    ApplicantSource,
    DocumentStatus,
    IssueSeverity,
    IssueStatus,
    SubmissionStatus,
    UserRole,
)
from tests.factories import (
    make_document,
    make_institution,
    make_requirement_set,
    make_stored_file,
    make_submission,
)


def test_full_object_graph_and_defaults(session: Session) -> None:
    sub = make_submission(session)
    doc = make_document(session, sub, make_stored_file(session))
    session.add_all(
        [
            DocumentPage(document_id=doc.id, page_number=1),
            ExtractedField(document_id=doc.id, field_key="candidate_name", machine_value="X"),
            ValidationIssue(
                submission_id=sub.id,
                code="missing_document",
                severity=IssueSeverity.ERROR,
                message="Degree certificate missing",
            ),
        ]
    )
    session.commit()

    session.expire_all()
    sub = session.get(Submission, sub.id)
    assert sub.status is SubmissionStatus.DRAFT
    assert sub.attributes == {}
    doc = sub.documents[0]
    assert doc.status is DocumentStatus.UPLOADED
    assert [p.page_number for p in doc.pages] == [1]
    assert doc.fields[0].field_key == "candidate_name"
    issue = session.scalars(select(ValidationIssue)).one()
    assert issue.status is IssueStatus.OPEN
    assert isinstance(sub.id, uuid.UUID)


def test_requirement_items_ordered_and_json_roundtrip(session: Session) -> None:
    rs = make_requirement_set(session, make_institution(session))
    session.commit()
    session.expire_all()
    rs = session.get(RequirementSet, rs.id)
    assert [i.key for i in rs.items] == ["ssc", "hsc_or_diploma"]
    assert rs.items[1].accepted_type_codes == ["hsc_marksheet", "diploma_marksheet"]


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(Submission(applicant_id=uuid.uuid4(), requirement_set_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.flush()


def test_external_ref_unique_per_institution(session: Session) -> None:
    inst = make_institution(session)
    other = make_institution(session, slug="other")
    for i in (inst, other):  # same ref in different tenants is fine
        session.add(
            Applicant(
                institution_id=i.id,
                external_ref="251200663",
                full_name="A",
                source=ApplicantSource.FORM_IMPORT,
            )
        )
    session.flush()

    session.add(
        Applicant(
            institution_id=inst.id,
            external_ref="251200663",
            full_name="B",
            source=ApplicantSource.FORM_IMPORT,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_applicant_cascades_but_keeps_stored_file(session: Session) -> None:
    sub = make_submission(session)
    f = make_stored_file(session)
    doc = make_document(session, sub, f)
    session.add(DocumentPage(document_id=doc.id, page_number=1))
    session.commit()

    session.delete(session.get(Applicant, sub.applicant_id))
    session.commit()

    assert session.scalars(select(Submission)).all() == []
    assert session.scalars(select(Document)).all() == []
    assert session.scalars(select(DocumentPage)).all() == []
    assert session.get(StoredFile, f.id) is not None  # blob cleanup is an explicit, audited step


def test_stored_file_in_use_cannot_be_deleted(session: Session) -> None:
    sub = make_submission(session)
    f = make_stored_file(session)
    make_document(session, sub, f)
    session.commit()
    session.delete(f)
    with pytest.raises(IntegrityError):
        session.commit()


def test_page_number_must_be_positive(session: Session) -> None:
    doc = make_document(session, make_submission(session), make_stored_file(session))
    session.add(DocumentPage(document_id=doc.id, page_number=0))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_confidence_range_enforced(session: Session, confidence: float) -> None:
    doc = make_document(session, make_submission(session), make_stored_file(session))
    session.add(ExtractedField(document_id=doc.id, field_key="x", confidence=confidence))
    with pytest.raises(IntegrityError):
        session.flush()


def test_unknown_enum_value_rejected(session: Session) -> None:
    session.add(User(email="a@example.com", full_name="A", role="superuser"))
    with pytest.raises((StatementError, LookupError)):
        session.flush()


def test_user_role_roundtrip(session: Session) -> None:
    inst = make_institution(session)
    session.add(
        User(email="v@example.com", full_name="V", role=UserRole.VERIFIER, institution_id=inst.id)
    )
    session.commit()
    assert session.scalars(select(User)).one().role is UserRole.VERIFIER
