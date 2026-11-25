"""IDs read identically from two different page images are confirmed; the confirmation goes
away with the page that gave it."""

from sqlalchemy.orm import Session

from app.extract.pipeline import CORROBORATED, corroborate_ids
from app.models import Document, DocumentPage, ExtractedField
from app.models.enums import FieldSource
from tests.factories import make_stored_file, make_submission


def _page(db: Session, doc: Document, n: int, image_sha: str) -> DocumentPage:
    image = make_stored_file(db, sha=image_sha * 64)
    page = DocumentPage(document_id=doc.id, page_number=n, image_file_id=image.id)
    db.add(page)
    db.flush()
    return page


def _id(db: Session, page: DocumentPage, value: str, conf: float = 0.75, **kw) -> ExtractedField:
    f = ExtractedField(document_id=page.document_id, page_id=page.id, field_key="student_id",
                       machine_value=value, value=value, confidence=conf,
                       bbox={"x": 1, "y": 2, "w": 3, "h": 4}, **kw)  # fmt: skip
    db.add(f)
    db.flush()
    return f


def test_an_id_on_two_page_images_is_confirmed_and_can_be_undone(session: Session) -> None:
    sub = make_submission(session)
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session, "f" * 64).id)
    session.add(doc)
    session.flush()
    p1, p2, p3 = (
        _page(session, doc, 1, "1"),
        _page(session, doc, 2, "2"),
        _page(session, doc, 3, "3"),
    )
    a, b = _id(session, p1, "2020015400296827"), _id(session, p2, "2020015400296827", 0.85)
    lone = _id(session, p3, "613167")

    assert corroborate_ids(session, sub.applicant_id) == 2
    assert a.confidence == b.confidence == CORROBORATED and lone.confidence == 0.75
    assert a.bbox["x"] == 1  # the box is kept

    session.delete(p2)  # its field goes with it
    session.flush()
    session.expire_all()
    assert corroborate_ids(session, sub.applicant_id) == 0
    a = session.get(ExtractedField, a.id)
    assert a.confidence == 0.75 and "base_confidence" not in a.bbox


def test_the_same_image_twice_is_not_a_second_reading(session: Session) -> None:
    # The same file uploaded into two slots gives the same misread twice.
    sub = make_submission(session)
    docs = []
    original = make_stored_file(session, "e" * 64)
    for _ in range(2):
        d = Document(submission_id=sub.id, file_id=original.id)
        session.add(d)
        session.flush()
        docs.append(d)
    image = make_stored_file(session, sha="9" * 64)
    fields = []
    for d in docs:
        page = DocumentPage(document_id=d.id, page_number=1, image_file_id=image.id)
        session.add(page)
        session.flush()
        fields.append(_id(session, page, "22JT310041"))
    assert corroborate_ids(session, sub.applicant_id) == 0
    assert all(f.confidence == 0.75 for f in fields)


def test_corrections_are_left_alone(session: Session) -> None:
    sub = make_submission(session)
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session, "d" * 64).id)
    session.add(doc)
    session.flush()
    machine = _id(session, _page(session, doc, 1, "4"), "S190944271")
    human = _id(session, _page(session, doc, 2, "5"), "S190944271", 1.0, source=FieldSource.HUMAN)
    assert corroborate_ids(session, sub.applicant_id) == 0  # a person's value is not a reading
    assert machine.confidence == 0.75 and human.confidence == 1.0


def _two_documents(db: Session, sub, second_sub=None) -> tuple[ExtractedField, Document]:
    """The same PRN on a page of each of two documents; returns (first field, second doc)."""
    fields, docs = [], []
    for n, s in enumerate((sub, second_sub or sub), start=1):
        d = Document(submission_id=s.id, file_id=make_stored_file(db, str(n) * 64).id)
        db.add(d)
        db.flush()
        fields.append(_id(db, _page(db, d, 1, "ab"[n - 1]), "2020015400296827"))
        docs.append(d)
    assert corroborate_ids(db, sub.applicant_id) == 2
    return fields[0], docs[1]


def test_removing_a_file_takes_its_confirmation_away(session: Session, test_settings) -> None:
    from app.core.security import load_storage_key
    from app.portal.uploads import remove_document
    from app.storage import EncryptedLocalStorage

    sub = make_submission(session)
    kept, other = _two_documents(session, sub)
    storage = EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))
    remove_document(session, storage, other, None)
    assert session.get(ExtractedField, kept.id).confidence == 0.75


def test_deleting_an_application_takes_its_confirmation_away(session: Session) -> None:
    from app.admin.erasure import delete_application
    from app.models import RequirementSet, Submission, User
    from app.models.enums import UserRole

    sub = make_submission(session)
    other_rs = RequirementSet(institution_id=sub.applicant.institution_id, code="other",
                              name="Another course", items=[])  # fmt: skip
    session.add(other_rs)
    session.flush()
    second = Submission(applicant_id=sub.applicant_id, requirement_set_id=other_rs.id)
    session.add(second)
    session.flush()
    kept, _ = _two_documents(session, sub, second)
    admin = User(email="a@example.test", full_name="A", role=UserRole.PLATFORM_ADMIN)
    session.add(admin)
    session.flush()
    delete_application(session, second, admin)
    assert session.get(ExtractedField, kept.id).confidence == 0.75
