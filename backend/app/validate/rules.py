"""Validation rules. Each rule reads a SubmissionContext and returns issue drafts.

Severity: error = very likely wrong (block until resolved); warning = a person must look;
info = worth knowing, nothing to do. Messages are written for the verifier and can be
shown to the student; details hold what the verifier needs to decide.
Thresholds were chosen on the seed data (docs/DATA_ANALYSIS.md, "Validation").
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.extract.fields import match_form_name
from app.models.enums import DocumentStatus, IssueSeverity, QualityVerdict, Readability
from app.validate.context import ID_FIELDS, DocInfo, PageInfo, SubmissionContext

ERROR, WARNING, INFO = IssueSeverity.ERROR, IssueSeverity.WARNING, IssueSeverity.INFO
IDENTITY_SLOTS = {"photo", "signature"}
ACADEMIC = ("ssc_marksheet", "hsc_marksheet", "diploma_marksheet", "semester_marksheet",
            "provisional_degree_certificate", "degree_certificate")  # fmt: skip
DEGREE_DOCS = ("provisional_degree_certificate", "degree_certificate")


@dataclass
class IssueDraft:
    code: str
    severity: IssueSeverity
    message: str
    key: str  # identifies the issue across re-runs (a dismissed issue stays dismissed)
    document_id: object | None = None
    details: dict = field(default_factory=dict)


Rule = Callable[[SubmissionContext], list[IssueDraft]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def accepted_types(item, attributes: dict) -> list[str]:
    """Types that satisfy a requirement item, given the submission's answers."""
    cond = item.condition or {}
    by_value = cond.get("accepted_by_value")
    if by_value:
        value = attributes.get(cond.get("attribute", ""))
        if value in by_value:
            return list(by_value[value])
    return list(item.accepted_type_codes or [])


# ---------- completeness ----------


@rule
def completeness(ctx: SubmissionContext) -> list[IssueDraft]:
    """Each required item must be present somewhere in the submission, judged by content
    (students often upload into the wrong slot). Identity images are judged by slot."""
    out = []
    present = {p.type_code for p in ctx.pages(*ACADEMIC)}
    for item in ctx.requirements:
        if not item.is_required:
            continue
        in_slot = [d for d in ctx.documents if d.slot == item.key]
        if item.key in IDENTITY_SLOTS:
            if not in_slot:
                out.append(IssueDraft(
                    "missing_document", ERROR, f"{item.label} not uploaded.",
                    f"missing:{item.key}", details={"item": item.key},
                ))  # fmt: skip
            continue
        wanted = accepted_types(item, ctx.attributes)
        if present & set(wanted):
            continue
        in_slot_types = {t for d in in_slot for t in d.types}
        substitutes = sorted(in_slot_types & set(item.accepted_type_codes or []))
        unrecognised = [d for d in in_slot if d.unrecognised_pages]
        if unrecognised:
            out.append(IssueDraft(
                "unconfirmed_document", WARNING,
                f"{item.label}: the uploaded file could not be recognised automatically; "
                "check that it is the right document.",
                f"unconfirmed:{item.key}", unrecognised[0].id,
                {"item": item.key, "expected_types": wanted},
            ))  # fmt: skip
        elif substitutes:
            # e.g. answered "already have the degree" but uploaded the provisional certificate
            out.append(IssueDraft(
                "expected_document_substituted", WARNING,
                f"{item.label}: expected {' or '.join(wanted)} for this application, found "
                f"{', '.join(substitutes)} instead.",
                f"substituted:{item.key}", in_slot[0].id if in_slot else None,
                {"item": item.key, "expected_types": wanted, "found": substitutes},
            ))  # fmt: skip
        else:
            found = sorted({t for d in in_slot for t in d.types})
            out.append(IssueDraft(
                "missing_document", ERROR, f"{item.label} not found among the uploaded documents.",
                f"missing:{item.key}", in_slot[0].id if in_slot else None,
                {"item": item.key, "expected_types": wanted, "found_in_slot": found},
            ))  # fmt: skip
    return out


@rule
def wrong_slot(ctx: SubmissionContext) -> list[IssueDraft]:
    """A recognised document uploaded into a slot that does not accept it."""
    out = []
    slots = {i.key: i for i in ctx.requirements}
    for d in ctx.documents:
        item = slots.get(d.slot)
        if item is None or d.slot in IDENTITY_SLOTS or not d.types:
            continue
        stray = d.types - set(item.accepted_type_codes or []) - {"supporting_page"}
        if stray:
            out.append(IssueDraft(
                "wrong_slot", INFO, f"{item.label}: contains {', '.join(sorted(stray))}.",
                f"wrong_slot:{d.id}", d.id, {"item": d.slot, "types": sorted(stray)},
            ))  # fmt: skip
    return out


@rule
def semester_coverage(ctx: SubmissionContext) -> list[IssueDraft]:
    """Enough semester pages for the highest semester seen. Semester numbers are read
    correctly on ~3 of 4 pages, so the check counts pages instead of looking for each
    number; missing numbers are only a hint."""
    pages = ctx.pages("semester_marksheet")
    numbers = {n for p in pages for n in p.semesters}
    if not numbers:
        return []
    highest = max(numbers)
    # Diploma holders usually join in the second year (lateral entry): semesters 1-2 are
    # not expected when no semester 1 or 2 marksheet was uploaded.
    lateral = bool(ctx.pages("diploma_marksheet")) and not numbers & {1, 2}
    expected = highest - 2 if lateral else highest
    covered = sum(max(1, len(p.semesters)) for p in pages)  # a year statement covers two
    if covered >= expected:
        return []
    return [IssueDraft(
        "semester_marksheets_missing", WARNING,
        f"Found marksheets for {covered} semester(s) but results up to semester {highest}; "
        "some semester marksheets may be missing.",
        "semesters", details={"highest": highest, "covered": covered, "lateral_entry": lateral,
                              "numbers_not_seen": sorted(set(range(3 if lateral else 1,
                                                                   highest + 1)) - numbers)},
    )]  # fmt: skip


# ---------- duplicates ----------

# The form allows the same document in both degree slots ("else provisional, else 8th sem");
# applicants still awaiting results upload their latest semester sheet there, which is
# also inside the semester PDF.
DEGREE_SLOTS = {"provisional_or_latest_sem", "degree_or_fallback"}
AWAITING = {"awaiting_provisional", "awaiting_final_semester"}


def _expected_repeat(slots: frozenset, degree_status: str | None) -> bool:
    allowed = DEGREE_SLOTS | ({"semester_marksheets"} if degree_status in AWAITING else set())
    return slots <= allowed


@rule
def duplicate_uploads(ctx: SubmissionContext) -> list[IssueDraft]:
    out = []
    by_hash: dict[str, list[DocInfo]] = {}
    for d in ctx.documents:
        by_hash.setdefault(d.sha256, []).append(d)
    for sha, docs in by_hash.items():
        if len(docs) > 1:
            slots = frozenset(d.slot for d in docs)
            expected = _expected_repeat(slots, ctx.attributes.get("degree_status"))
            out.append(IssueDraft(
                "duplicate_upload", INFO if expected else WARNING,
                "The same file was uploaded in several places"
                + (" (allowed by the form)." if expected else "; a document may be missing."),
                f"dup:{sha[:16]}", docs[0].id, {"slots": sorted(s or "" for s in slots)},
            ))  # fmt: skip
        if ctx.shared_hashes.get(sha):
            out.append(IssueDraft(
                "file_shared_with_other_applicant", ERROR,
                "This exact file was also submitted by another applicant.",
                f"shared_file:{sha[:16]}", docs[0].id,
                {"other_applicants": ctx.shared_hashes[sha]},
            ))  # fmt: skip
    return out


@rule
def shared_identifiers(ctx: SubmissionContext) -> list[IssueDraft]:
    out = []
    for value, n in sorted(ctx.shared_ids.items()):
        doc = next((d for d in ctx.documents for p in d.pages
                    if any(p.get(k) == value for k in ID_FIELDS)), None)  # fmt: skip
        out.append(IssueDraft(
            "identifier_shared_with_other_applicant", ERROR,
            "A seat/registration number on this document also appears on another applicant's "
            "document.", f"shared_id:{value}", doc.id if doc else None,
            {"identifier": value, "other_applicants": n},
        ))  # fmt: skip
    return out


# ---------- identity ----------


@rule
def name_matches_form(ctx: SubmissionContext) -> list[IssueDraft]:
    """The applicant's name must appear on each academic document. Judged per document:
    one page with the full name is enough (back sides and continuation pages often carry no
    name, and a misread name on one page of an 8-page PDF is not a mismatch)."""
    out = []
    for d in ctx.documents:
        readings = []
        for p in d.pages:
            if _name_unsettled(p):
                continue
            if p.type_code in ACADEMIC and (printed := p.get("candidate_name")):
                _, matched, total = match_form_name(printed, ctx.applicant_name)
                if total:
                    readings.append((matched / total, matched, total, p, printed))
        if not readings:
            continue
        share, matched, total, p, printed = max(readings, key=lambda r: r[0])
        if share == 1 or any(_first_and_surname(ctx.applicant_name, r[4]) for r in readings):
            continue
        mismatch = matched == 0
        out.append(IssueDraft(
            "name_mismatch" if mismatch else "name_partial_match",
            ERROR if mismatch else WARNING,
            "The name on this document does not match the application."
            if mismatch else "The name on this document only partly matches the application.",
            f"name:{d.id}", d.id,
            {"page": p.number, "printed_name": printed,
             "form_words_matched": f"{matched}/{total}"},
        ))  # fmt: skip
    return out


def _name_unsettled(p: PageInfo) -> bool:
    """A name the readability check doubts is not compared: a misread name is a reading
    problem (handled by asking the student), not a mismatch hinting at someone else's
    document."""
    if p.readability == Readability.RETAKE:
        return True
    return p.readability == Readability.CONFIRM and "candidate_name" in (
        p.readability_details.get("ask") or []
    )


def _first_and_surname(form_name: str, printed: str) -> bool:
    """The form asks for "First Middle Last"; documents often drop the middle (father's)
    name, so first name + surname on the document is a match."""
    form = form_name.upper().split()
    if len(form) < 3:
        return False
    fixed, _, _ = match_form_name(printed, form_name)
    words = set(fixed.split())
    return form[0] in words and form[-1] in words


# ---------- marks & grades ----------


@rule
def marks_consistent(ctx: SubmissionContext) -> list[IssueDraft]:
    """Total / maximum must equal the printed percentage (a sign of editing or misreading)."""
    out = []
    for d in ctx.documents:
        for p in d.pages:
            # Only confident (or reviewer-confirmed) values: the extractor already lowers the
            # confidence of totals and percentages that disagree, routing them to review.
            t, m, pct = (p.get(k, 0.9) for k in ("total_marks_obtained", "total_marks_max",
                                                 "percentage"))  # fmt: skip
            if not (t and m and pct):
                continue
            computed = int(t) / int(m) * 100
            if abs(computed - float(pct)) > 1.0:
                out.append(IssueDraft(
                    "marks_inconsistent", WARNING,
                    "Total marks and percentage on this page do not agree.",
                    f"marks:{p.id}", d.id,
                    {"page": p.number, "total": t, "maximum": m, "percentage": pct,
                     "computed_percentage": round(computed, 2)},
                ))  # fmt: skip
    return out


# CGPA -> percentage conversions in use by Indian universities (the form does not say which
# the applicant used): x10, x9.5 (CBSE/AICTE), (x-0.75)x10 (SPPU, Mumbai older), 7.1x+11.
CONVERSIONS = (lambda g: g * 10, lambda g: g * 9.5, lambda g: (g - 0.75) * 10,
               lambda g: 7.1 * g + 11)  # fmt: skip
PERCENT_TOLERANCE = 1.0
CGPA_TOLERANCE = 1.5


def final_cgpa(ctx: SubmissionContext) -> tuple[float, str] | None:
    """The applicant's final CGPA: from a degree/provisional certificate, else from the
    semester marksheet with the highest semester number."""
    for p in ctx.pages(*DEGREE_DOCS):
        if v := p.get("cgpa", 0.75):
            return float(v), p.type_code
    sems = [p for p in ctx.pages("semester_marksheet") if p.get("cgpa", 0.75) and p.semesters]
    if sems:
        last = max(sems, key=lambda p: max(p.semesters))
        highest = max(n for p in ctx.pages("semester_marksheet") for n in p.semesters)
        # A CGPA from an earlier semester than the latest marksheet is not the final one.
        if max(last.semesters) == highest:
            return float(last.get("cgpa")), f"semester {highest}"
    return None


@rule
def claimed_percentage(ctx: SubmissionContext) -> list[IssueDraft]:
    claimed = ctx.attributes.get("claimed_aggregate_percent")
    if claimed is None:
        return []
    pcts = [float(v) for p in ctx.pages(*DEGREE_DOCS) if (v := p.get("percentage", 0.75))]
    if pcts:
        if any(abs(v - claimed) <= PERCENT_TOLERANCE for v in pcts):
            return []
        return [IssueDraft(
            "claimed_percentage_mismatch", WARNING,
            "The aggregate percentage in the application differs from the degree documents.",
            "claimed_pct", details={"claimed": claimed, "documents": pcts},
        )]  # fmt: skip
    found = final_cgpa(ctx)
    if found is None:
        return [IssueDraft(
            "claimed_percentage_unverified", INFO,
            "The aggregate percentage could not be checked: no final CGPA or percentage was "
            "read from the documents.", "claimed_pct", details={"claimed": claimed},
        )]  # fmt: skip
    cgpa, source = found
    options = [round(f(cgpa), 2) for f in CONVERSIONS]
    if min(options) - CGPA_TOLERANCE <= claimed <= max(options) + CGPA_TOLERANCE:
        return []
    return [IssueDraft(
        "claimed_percentage_mismatch", WARNING,
        "The aggregate percentage in the application is not consistent with the CGPA on the "
        "documents under any common conversion.",
        "claimed_pct", details={"claimed": claimed, "cgpa": cgpa, "cgpa_from": source,
                                "conversions": options},
    )]  # fmt: skip


# ---------- quality ----------


@rule
def unreadable(ctx: SubmissionContext) -> list[IssueDraft]:
    out = []
    for d in ctx.documents:
        if d.status == DocumentStatus.FAILED:
            out.append(IssueDraft("document_unreadable", ERROR,
                                  "This file could not be opened; ask for a new upload.",
                                  f"failed:{d.id}", d.id))  # fmt: skip
            continue
        bad = [p.number for p in d.pages if p.quality == QualityVerdict.RETAKE]
        if bad and d.slot not in IDENTITY_SLOTS:
            out.append(IssueDraft(
                "page_quality_poor", WARNING,
                "Some pages are too blurry or small to read reliably; a clearer copy is needed.",
                f"quality:{d.id}", d.id, {"pages": bad},
            ))  # fmt: skip
        out.extend(_readability(d, bad))
    return out


def _readability(d: DocInfo, image_retake: list[int]) -> list[IssueDraft]:
    """How well the text was read (app/extract/readability.py). Pages already asked to be
    retaken for image quality are not reported twice."""
    out = []
    by = {}
    for p in d.pages:
        if p.number not in image_retake and p.readability:
            by.setdefault(p.readability, []).append(p)
    if pages := by.get(Readability.RETAKE):
        out.append(IssueDraft(
            "page_text_unreadable", WARNING,
            "The text on some pages could not be read reliably (or the values the student "
            "typed did not match it); a clearer copy is needed.",
            f"readability_retake:{d.id}", d.id, {"pages": [p.number for p in pages]},
        ))  # fmt: skip
    if pages := by.get(Readability.CONFIRM):
        out.append(IssueDraft(
            "page_needs_confirmation", WARNING,
            "Some values could not be read with confidence; waiting for the student to type "
            "them as printed.",
            f"readability_confirm:{d.id}", d.id,
            {"pages": [p.number for p in pages],
             "fields": sorted({k for p in pages for k in p.readability_details.get("ask", [])})},
        ))  # fmt: skip
    if pages := by.get(Readability.CONFIRMED):
        loose = sorted({k for p in pages for k in (p.readability_details.get("inexact", [])
                        + p.readability_details.get("not_printed", []))})  # fmt: skip
        out.append(IssueDraft(
            "student_confirmed_values", WARNING if loose else INFO,
            "The student typed values the machine could not read clearly; they match the "
            "page" + (" only approximately or were marked as not printed: check them against "
                      "the image." if loose else "."),
            f"readability_confirmed:{d.id}", d.id,
            {"pages": [p.number for p in pages],
             "fields": sorted({k for p in pages for k in p.readability_details.get("ask", [])}),
             "check": loose},
        ))  # fmt: skip
    return out


def run_rules(ctx: SubmissionContext) -> list[IssueDraft]:
    issues: list[IssueDraft] = []
    for r in RULES:
        issues.extend(r(ctx))
    return issues
