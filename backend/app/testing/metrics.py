"""Metrics for a set of submissions: a Testing Lab run, or the real applicants as the
baseline to compare against. Counts and rates only, never names or values.

Every metric is ``{"key", "label", "value", "unit", "n", "better"}``: ``n`` is what the
rate is out of (so a 100% of 2 is not mistaken for a 100% of 200), ``better`` says which
direction is an improvement ("higher", "lower" or None).

Accuracy comes from review in the verifier console: a value the reviewer confirmed (kept as
read) was right, one they changed was wrong, one they had to add was missed. A page's type
was right unless the reviewer changed it. Only reviewed values and pages count, so the
numbers firm up as more is reviewed.
"""

import uuid
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    Job,
    PageOcr,
    Submission,
    ValidationIssue,
)
from app.models.enums import (
    ClassificationMethod,
    DocumentCategory,
    DocumentStatus,
    FieldSource,
    IssueStatus,
    JobStatus,
    OcrStatus,
    QualityVerdict,
    Readability,
)
from app.ocr.pipeline import ENGINE_PREFERENCE
from app.validate.engine import verdict

HIGH_CONFIDENCE = 0.9
NAME = "candidate_name"
ID_KEYS = ("seat_number", "student_id")
GRADE_KEYS = ("sgpa", "cgpa", "percentage", "total_marks_obtained")
ACCURACY_KEYS = (
    (NAME, "Name"),
    ("seat_number", "Seat number"),
    ("student_id", "PRN / enrolment no."),
    ("sgpa", "SGPA"),
    ("cgpa", "CGPA"),
    ("percentage", "Percentage"),
    ("total_marks_obtained", "Total marks"),
    ("result", "Result"),
)
METHOD_LABELS = {
    ClassificationMethod.CONTENT_EXACT: "title found",
    ClassificationMethod.CONTENT_FUZZY: "title found (approx.)",
    ClassificationMethod.PHRASE: "admin title phrase",
    ClassificationMethod.MODEL: "page-type model",
    ClassificationMethod.CONTINUATION: "same as previous page",
    ClassificationMethod.SLOT_HINT: "upload slot",
    ClassificationMethod.HUMAN: "set by a reviewer",
}


def _m(key, label, value, unit="%", n=None, better=None) -> dict:
    if unit == "%" and value is not None:
        value = round(100 * value, 1)
    elif isinstance(value, float):
        value = round(value, 2)
    return {"key": key, "label": label, "value": value, "unit": unit, "n": n, "better": better}


def _rate(part: int, whole: int):
    return part / whole if whole else None


def baseline_ids(db: Session) -> list[uuid.UUID]:
    """The real applicants (everything outside the Testing Lab)."""
    return list(db.scalars(select(Submission.id).where(Submission.test_run_id.is_(None))))


def compute(db: Session, submission_ids: list[uuid.UUID]) -> dict:
    ids = list(submission_ids)
    docs = db.scalars(select(Document).where(Document.submission_id.in_(ids))).all() if ids else []
    doc_ids = [d.id for d in docs]
    pages = (
        db.scalars(select(DocumentPage).where(DocumentPage.document_id.in_(doc_ids))).all()
        if doc_ids
        else []
    )
    page_ids = [p.id for p in pages]
    types = {t.id: t for t in db.scalars(select(DocumentType))}
    academic = [
        p
        for p in pages
        if p.detected_type_id
        and types[p.detected_type_id].category == DocumentCategory.ACADEMIC_RECORD
    ]

    # ---- processing ----
    jobs = db.scalars(select(Job).where(Job.document_id.in_(doc_ids))).all() if doc_ids else []
    waiting = {j.document_id for j in jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)}
    # Only documents processed by the worker have job times (the seed data was batch-run).
    timed = [j for j in jobs if j.status == JobStatus.DONE and j.started_at and j.finished_at]
    busy_s = sum((j.finished_at - j.started_at).total_seconds() for j in timed)
    timed_docs = {j.document_id for j in timed}
    done_pages = sum(1 for p in pages if p.document_id in timed_docs)
    ocr_ms = (
        db.scalar(select(func.sum(PageOcr.timing_ms)).where(PageOcr.page_id.in_(page_ids)))
        if page_ids
        else None
    )
    failed = sum(1 for d in docs if d.status == DocumentStatus.FAILED)
    overview = [
        _m("students", "Students", len(ids), "count"),
        _m("documents", "Documents", len(docs), "count"),
        _m("pages", "Pages", len(pages), "count"),
        _m("processing", "Documents still being processed", len(waiting), "count"),
        _m(
            "failed",
            "Files that could not be opened",
            _rate(failed, len(docs)),
            n=len(docs),
            better="lower",
        ),  # fmt: skip
        _m(
            "job_s_per_page",
            "Processing time per page",
            busy_s / done_pages if done_pages and busy_s else None,
            "s",
            n=done_pages,
            better="lower",
        ),  # fmt: skip
        _m(
            "ocr_s_per_page",
            "of which reading (OCR)",
            (ocr_ms or 0) / 1000 / len(pages) if pages and ocr_ms else None,
            "s",
            n=len(pages),
            better="lower",
        ),  # fmt: skip
    ]

    # ---- picture quality ----
    judged = [p for p in pages if p.quality_verdict and p.quality_verdict != QualityVerdict.BLANK]
    q = Counter(p.quality_verdict for p in judged)
    picture = [
        _m(
            "quality_ok",
            "Pictures fine",
            _rate(q[QualityVerdict.OK], len(judged)),
            n=len(judged),
            better="higher",
        ),  # fmt: skip
        _m(
            "quality_warn",
            "Pictures with a warning (small, faded, slightly blurry)",
            _rate(q[QualityVerdict.WARN], len(judged)),
            n=len(judged),
            better="lower",
        ),  # fmt: skip
        _m(
            "quality_retake",
            "Pictures too poor to read (retake)",
            _rate(q[QualityVerdict.RETAKE], len(judged)),
            n=len(judged),
            better="lower",
        ),  # fmt: skip
    ]

    # ---- reading ----
    confs = _preferred_confidences(db, page_ids)
    assessed = [p for p in academic if p.readability]
    r = Counter(p.readability for p in assessed)
    reading = [
        _m(
            "ocr_confidence",
            "Average OCR confidence",
            sum(confs) / len(confs) if confs else None,
            "score",
            n=len(confs),
            better="higher",
        ),  # fmt: skip
        _m(
            "read_clear",
            "Pages read clearly",
            _rate(r[Readability.CLEAR] + r[Readability.CONFIRMED], len(assessed)),
            n=len(assessed),
            better="higher",
        ),  # fmt: skip
        _m(
            "read_confirm",
            "Pages where the student is asked to type values",
            _rate(r[Readability.CONFIRM], len(assessed)),
            n=len(assessed),
            better="lower",
        ),  # fmt: skip
        _m(
            "read_retake",
            "Pages whose text could not be read (retake)",
            _rate(r[Readability.RETAKE], len(assessed)),
            n=len(assessed),
            better="lower",
        ),  # fmt: skip
    ]

    # ---- recognition ----
    readable = [p for p in pages if p.quality_verdict != QualityVerdict.BLANK and p.image_file_id
                and not _identity(p, docs)]  # fmt: skip
    typed = [p for p in readable if p.detected_type_id]
    methods = Counter(p.classification_method for p in typed if p.classification_method)
    recognition = [
        _m(
            "recognised",
            "Pages recognised (document type known)",
            _rate(len(typed), len(readable)),
            n=len(readable),
            better="higher",
        ),  # fmt: skip
        *[
            _m(
                f"method_{m.value}",
                f"recognised by: {label}",
                _rate(methods[m], len(typed)),
                n=len(typed),
            )
            for m, label in METHOD_LABELS.items()
            if methods[m]
        ],
    ]
    by_type = Counter(types[p.detected_type_id].name for p in typed)

    # ---- values found ----
    fields = (
        db.scalars(select(ExtractedField).where(ExtractedField.page_id.in_(page_ids))).all()
        if page_ids
        else []
    )
    by_page: dict = {}
    for f in fields:
        by_page.setdefault(f.page_id, {})[f.field_key] = f

    def found(keys) -> tuple[int, int]:  # noqa: E306
        have = [p for p in academic if any(k in by_page.get(p.id, {}) for k in keys)]
        sure = [p for p in have if any((by_page[p.id][k].confidence or 0) >= HIGH_CONFIDENCE
                                       for k in keys if k in by_page[p.id])]  # fmt: skip
        return len(have), len(sure)

    values = []
    for key, label, keys in (("name", "Name", (NAME,)), ("id", "Seat no. / PRN", ID_KEYS),
                             ("grade", "SGPA / CGPA / % / total", GRADE_KEYS)):  # fmt: skip
        have, sure = found(keys)
        values.append(_m(f"found_{key}", f"{label} found", _rate(have, len(academic)),
                         n=len(academic), better="higher"))  # fmt: skip
        values.append(_m(f"sure_{key}", f"{label} found with high confidence",
                         _rate(sure, len(academic)), n=len(academic), better="higher"))  # fmt: skip

    # ---- outcome for students and staff ----
    issues = (
        db.scalars(select(ValidationIssue).where(ValidationIssue.submission_id.in_(ids))).all()
        if ids
        else []
    )
    open_by_sub: dict = {}
    for i in issues:
        if i.status == IssueStatus.OPEN:
            open_by_sub.setdefault(i.submission_id, []).append(i)
    verdicts = Counter(verdict([i.severity for i in open_by_sub.get(s, [])]) for s in ids)
    pages_by_sub: dict = {}
    doc_sub = {d.id: d.submission_id for d in docs}
    for p in pages:
        pages_by_sub.setdefault(doc_sub[p.document_id], []).append(p)
    asked = sum(
        any(p.readability == Readability.CONFIRM for p in pages_by_sub.get(s, [])) for s in ids
    )
    retake = sum(
        any(
            p.readability == Readability.RETAKE or p.quality_verdict == QualityVerdict.RETAKE
            for p in pages_by_sub.get(s, [])
        )  # fmt: skip
        for s in ids
    )
    name_problem = sum(
        any(i.code in ("name_mismatch", "name_partial_match") for i in open_by_sub.get(s, []))
        for s in ids
    )
    outcome = [
        _m(
            "verdict_clear",
            "Students clear (no review needed)",
            _rate(verdicts["clear"], len(ids)),
            n=len(ids),
            better="higher",
        ),  # fmt: skip
        _m(
            "verdict_review",
            "Students needing a verifier's look",
            _rate(verdicts["review"], len(ids)),
            n=len(ids),
            better="lower",
        ),  # fmt: skip
        _m(
            "verdict_problems",
            "Students with problems",
            _rate(verdicts["problems"], len(ids)),
            n=len(ids),
            better="lower",
        ),  # fmt: skip
        _m(
            "students_asked",
            "Students asked to type values",
            _rate(asked, len(ids)),
            n=len(ids),
            better="lower",
        ),  # fmt: skip
        _m(
            "students_retake",
            "Students asked for a clearer copy",
            _rate(retake, len(ids)),
            n=len(ids),
            better="lower",
        ),  # fmt: skip
        _m(
            "name_problem",
            "Students whose name did not (fully) match",
            _rate(name_problem, len(ids)),
            n=len(ids),
            better="lower",
        ),  # fmt: skip
    ]
    issue_counts = Counter(
        i.code for subs in open_by_sub.values() for i in {i.code: i for i in subs}.values()
    )

    return {
        "sections": [
            {"key": "overview", "title": "Processing", "metrics": overview},
            {"key": "picture", "title": "Picture quality", "metrics": picture},
            {"key": "reading", "title": "Reading the text", "metrics": reading},
            {"key": "recognition", "title": "Recognising documents", "metrics": recognition},
            {"key": "values", "title": "Finding values (academic pages)", "metrics": values},
            {"key": "outcome", "title": "Outcome for students and staff", "metrics": outcome},
            {
                "key": "accuracy",
                "title": "Accuracy (from your reviews)",
                "metrics": _accuracy(fields, pages, types),
            },  # fmt: skip
        ],
        "document_types": dict(by_type.most_common()),
        "issues": dict(issue_counts.most_common()),  # students per open issue
    }


def _preferred_confidences(db: Session, page_ids: list) -> list[float]:
    """Mean OCR confidence of each page's preferred reading (columns only: the stored words
    are large and not needed here)."""
    if not page_ids:
        return []
    rank = {name: i for i, name in enumerate(ENGINE_PREFERENCE)}
    best: dict = {}
    for pid, engine, status, conf in db.execute(
        select(PageOcr.page_id, PageOcr.engine, PageOcr.status, PageOcr.mean_confidence).where(
            PageOcr.page_id.in_(page_ids)
        )
    ):
        key = (status == OcrStatus.FAILED, rank.get(engine, len(rank)))
        if pid not in best or key < best[pid][0]:
            best[pid] = (key, conf)
    return [c for _, c in best.values() if c is not None]


def _identity(p: DocumentPage, docs) -> bool:
    slot = next((d.slot_key for d in docs if d.id == p.document_id), None)
    return slot in ("photo", "signature")


def _accuracy(fields: list[ExtractedField], pages: list[DocumentPage], types: dict) -> list[dict]:
    reviewed = [f for f in fields if f.source == FieldSource.HUMAN and f.corrected_by_id]
    out = []
    right_all = wrong_all = 0
    sure_right = sure_n = 0
    for key, label in ACCURACY_KEYS:
        mine = [f for f in reviewed if f.field_key == key]
        read = [f for f in mine if f.machine_value is not None]
        right = sum(1 for f in read if f.value == f.machine_value)
        missed = len(mine) - len(read)
        right_all += right
        wrong_all += len(read) - right
        sure = [f for f in read if (f.confidence or 0) >= HIGH_CONFIDENCE]
        sure_n += len(sure)
        sure_right += sum(1 for f in sure if f.value == f.machine_value)
        if mine:
            out.append(_m(f"acc_{key}", f"{label}: read correctly", _rate(right, len(read)),
                          n=len(read), better="higher"))  # fmt: skip
            if missed:
                out.append(_m(f"missed_{key}", f"{label}: missed (added by reviewer)", missed,
                              "count", better="lower"))  # fmt: skip
    out.insert(0, _m("acc_all", "All reviewed values read correctly",
                     _rate(right_all, right_all + wrong_all), n=right_all + wrong_all,
                     better="higher"))  # fmt: skip
    out.insert(1, _m("acc_sure", "High-confidence values that were right",
                     _rate(sure_right, sure_n), n=sure_n, better="higher"))  # fmt: skip

    # Page types: a page counts as reviewed when a reviewer touched a value on it or set its
    # type; its type was right unless the reviewer changed it.
    touched = {f.page_id for f in reviewed}
    codes = {t.id: t.code for t in types.values()}
    reviewed_pages = [
        p for p in pages if p.id in touched or p.classification_method == ClassificationMethod.HUMAN
    ]
    wrong_type = 0
    for p in reviewed_pages:
        machine = next(
            (e[8:] for e in p.classification_evidence or [] if e.startswith("machine:")), None
        )
        if machine is not None and machine != codes.get(p.detected_type_id):
            wrong_type += 1
    out.insert(2, _m("acc_type", "Page type correct", _rate(len(reviewed_pages) - wrong_type,
                     len(reviewed_pages)), n=len(reviewed_pages), better="higher"))  # fmt: skip
    out.insert(3, _m("reviewed_pages", "Pages reviewed", len(reviewed_pages), "count",
                     n=len(pages)))  # fmt: skip
    return out
