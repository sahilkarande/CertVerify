"""Classify every page of a document and store the result.

Order of evidence (strongest first):
1. content: the page's own OCR text matches a type's rules;
2. continuation: an unreadable later page gets the type of the page before it
   (back sides, continuation sheets);
3. slot hint: an unreadable page uploaded into a slot that accepts exactly one academic type.
Anything else stays unknown (no type) and goes to human review.
"""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.rules import Classification, classify_text
from app.models import Document, DocumentPage, DocumentType, PageOcr, RequirementItem, Submission
from app.models.enums import ClassificationMethod, DocumentCategory
from app.ocr.pipeline import preferred_readings

# Bump when rules or fallbacks change so pages are re-classified.
CLASSIFIER_VERSION = 7

CONTINUATION_CONFIDENCE = 0.5
SLOT_HINT_CONFIDENCE = 0.4


@dataclass
class DocResult:
    pages: int
    methods: Counter


def _slot_single_type(db: Session, doc: Document) -> str | None:
    """The only academic type the document's upload slot accepts, if there is exactly one."""
    if not doc.slot_key:
        return None
    item = db.scalar(
        select(RequirementItem)
        .join(Submission, Submission.requirement_set_id == RequirementItem.requirement_set_id)
        .where(Submission.id == doc.submission_id, RequirementItem.key == doc.slot_key)
    )
    if item is None:
        return None
    academic = list(
        db.scalars(
            select(DocumentType.code).where(
                DocumentType.code.in_(item.accepted_type_codes),
                DocumentType.category == DocumentCategory.ACADEMIC_RECORD,
            )
        )
    )
    return academic[0] if len(academic) == 1 else None


def _apply(
    page: DocumentPage,
    c: Classification,
    method: ClassificationMethod | None,
    type_ids: dict[str, object],
) -> None:
    new_type = type_ids.get(c.type_code) if c.type_code else None
    if new_type != page.detected_type_id:
        page.extractor_version = None  # fields depend on the type: extract again
    page.detected_type_id = new_type
    page.classification_method = method
    page.classification_confidence = c.confidence if c.type_code else None
    page.semesters = c.semesters
    page.academic_level = c.level
    page.classification_evidence = c.evidence
    page.classifier_version = CLASSIFIER_VERSION


def classify_document(db: Session, doc: Document, type_ids: dict[str, object]) -> DocResult:
    """Classify the document's OCR'd pages and set the document's type if they all agree."""
    pages = db.scalars(
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    ).all()
    readings = preferred_readings(db, [p.id for p in pages])
    rows = [(p, readings[p.id].text, readings[p.id].words) for p in pages if p.id in readings]
    methods: Counter = Counter()
    slot_type = _slot_single_type(db, doc)
    previous: Classification | None = None
    for page, text, words in rows:
        c = classify_text(text, words)
        if c.type_code:
            method = (
                ClassificationMethod.CONTENT_FUZZY
                if c.fuzzy
                else ClassificationMethod.CONTENT_EXACT
            )
        elif previous is not None and previous.type_code:
            c = Classification(
                type_code=previous.type_code,
                confidence=CONTINUATION_CONFIDENCE,
                level=previous.level,
                evidence=[f"continuation_of_page:{page.page_number - 1}", *c.evidence],
            )
            method = ClassificationMethod.CONTINUATION
        elif slot_type:
            c = Classification(
                type_code=slot_type,
                confidence=SLOT_HINT_CONFIDENCE,
                evidence=[f"slot_hint:{doc.slot_key}", *c.evidence],
            )
            method = ClassificationMethod.SLOT_HINT
        else:
            method = None
        _apply(page, c, method, type_ids)
        methods[method.value if method else "unknown"] += 1
        previous = c

    types = {p.detected_type_id for p, _, _ in rows}
    doc.detected_type_id = types.pop() if len(types) == 1 else None
    db.flush()
    return DocResult(pages=len(rows), methods=methods)


def type_id_map(db: Session) -> dict[str, object]:
    return dict(db.execute(select(DocumentType.code, DocumentType.id)).all())


def documents_needing_classification(db: Session, *, force: bool = False) -> list:
    """Ids of documents with OCR'd pages whose classification is missing or outdated."""
    q = (
        select(DocumentPage.document_id)
        .join(PageOcr, PageOcr.page_id == DocumentPage.id)
        .distinct()
    )
    if not force:
        q = q.where(
            DocumentPage.classifier_version.is_(None)
            | (DocumentPage.classifier_version != CLASSIFIER_VERSION)
        )
    return list(db.scalars(q))
