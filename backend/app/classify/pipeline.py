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

from app.classify.rules import Classification, classification_for, classify_text
from app.classify.text import compact, header_text
from app.models import Document, DocumentPage, DocumentType, PageOcr, RequirementItem, Submission
from app.models.enums import ClassificationMethod, DocumentCategory
from app.ocr.pipeline import preferred_readings

# Bump when rules or fallbacks change so pages are re-classified.
CLASSIFIER_VERSION = 9

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


# The trained page-type model (when one is active) types pages the rules could not, and
# questions confident rule results it strongly disagrees with. Measured on this data: right on
# all 8 unrecognised pages it was >= 0.6 sure of, and on all 5 pages where it overruled a
# continuation/slot guess at >= 0.8 (docs/DATA_ANALYSIS.md, "Learning loop").
MODEL_MIN_PROBABILITY = 0.6
MODEL_OVER_CONTINUATION = 0.8  # measured: a 0.60 guess wrongly overruled a back side
MODEL_MAX_CONFIDENCE = 0.85  # model-typed pages always go to a person
MODEL_DISAGREE_PROBABILITY = 0.9
DISAGREEMENT_CONFIDENCE = 0.7


@dataclass
class ModelRef:
    model: object  # PageTypeModel
    version: int


PHRASE_CONFIDENCE = 0.9


def phrase_rules(db: Session) -> list[tuple[str, list[str]]]:
    """Title phrases admins defined for document types (recognition without code)."""
    out = []
    for code, rec in db.execute(
        select(DocumentType.code, DocumentType.recognition).where(DocumentType.is_active.is_(True))
    ):
        phrases = [compact(p) for p in (rec or {}).get("title_phrases", []) if compact(p)]
        if phrases:
            out.append((code, phrases))
    return out


def _phrase_match(rules: list[tuple[str, list[str]]], words: list) -> tuple[str, str] | None:
    header = compact(header_text(words))
    for code, phrases in rules:
        for phrase in phrases:
            if phrase in header:
                return code, phrase
    return None


def classify_document(
    db: Session,
    doc: Document,
    type_ids: dict[str, object],
    model: ModelRef | None = None,
    phrases: list[tuple[str, list[str]]] | None = None,
) -> DocResult:
    """Classify the document's OCR'd pages and set the document's type if they all agree.
    Pages a verifier typed are kept as they are."""
    pages = db.scalars(
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    ).all()
    readings = preferred_readings(db, [p.id for p in pages])
    rows = [(p, readings[p.id].text, readings[p.id].words) for p in pages if p.id in readings]
    codes = {v: k for k, v in type_ids.items()}
    methods: Counter = Counter()
    slot_type = _slot_single_type(db, doc)
    previous: Classification | None = None
    for page, text, words in rows:
        if page.classification_method == ClassificationMethod.HUMAN:
            previous = Classification(type_code=codes.get(page.detected_type_id), confidence=1.0)
            methods["human"] += 1
            continue
        c = classify_text(text, words)
        if not c.type_code and phrases and (hit := _phrase_match(phrases, words)):
            c = classification_for(hit[0], text, words, PHRASE_CONFIDENCE, [f"phrase:{hit[1]}"])
            c.fuzzy = False
        guess = model.model.predict(text) if model is not None else None
        tag = f"model:v{model.version}:{guess[0]}:{guess[1]:.2f}" if guess else None
        if c.type_code:
            method = (
                ClassificationMethod.PHRASE
                if c.evidence and str(c.evidence[0]).startswith("phrase:")
                else ClassificationMethod.CONTENT_FUZZY
                if c.fuzzy
                else ClassificationMethod.CONTENT_EXACT
            )
            if guess and guess[0] != c.type_code and guess[1] >= MODEL_DISAGREE_PROBABILITY:
                c.confidence = min(c.confidence, DISAGREEMENT_CONFIDENCE)
                c.evidence.append(f"model_disagrees:{tag}")
        elif guess and (
            guess[1] >= MODEL_OVER_CONTINUATION
            or (guess[1] >= MODEL_MIN_PROBABILITY and not (previous and previous.type_code))
        ):
            # A back side or continuation sheet is usually the previous page's type: the
            # model overrules that only when it is quite sure.
            c = classification_for(
                guess[0],
                text,
                words,
                round(min(guess[1], MODEL_MAX_CONFIDENCE), 2),
                [tag, *c.evidence],
            )
            method = ClassificationMethod.MODEL
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


def load_model(db: Session, models_dir) -> ModelRef | None:
    from app.learn.registry import active_page_type_model

    found = active_page_type_model(db, models_dir)
    return ModelRef(*found) if found else None


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
