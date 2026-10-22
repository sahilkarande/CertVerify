"""Run the rules for a submission and store the result as ValidationIssue rows.

Re-running is safe: open machine issues are replaced; an issue a person resolved or
dismissed keeps its status and is not raised again while the same problem remains.
"""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Submission, ValidationIssue
from app.models.enums import IssueSeverity, IssueStatus, SubmissionStatus
from app.validate.context import load_context
from app.validate.rules import run_rules

# Bump when rules change, so reports can tell which rules produced an issue.
RULES_VERSION = 1


@dataclass
class ValidationResult:
    issues: Counter  # code -> count of open issues
    verdict: str  # "clear" | "review" | "problems"


def verdict(severities: list[IssueSeverity]) -> str:
    if IssueSeverity.ERROR in severities:
        return "problems"
    if IssueSeverity.WARNING in severities:
        return "review"
    return "clear"


def validate_submission(db: Session, submission: Submission) -> ValidationResult:
    """Validate one submission. Caller commits."""
    drafts = run_rules(load_context(db, submission))
    existing = db.scalars(
        select(ValidationIssue).where(ValidationIssue.submission_id == submission.id)
    ).all()
    decided = {i.details.get("key") for i in existing if i.status != IssueStatus.OPEN}
    for issue in existing:
        if issue.status == IssueStatus.OPEN:
            db.delete(issue)
    db.flush()
    open_now = []
    for d in drafts:
        if d.key in decided:
            continue
        db.add(
            ValidationIssue(
                submission_id=submission.id,
                document_id=d.document_id,
                code=d.code,
                severity=d.severity,
                message=d.message,
                details={**d.details, "key": d.key, "rules_version": RULES_VERSION},
            )
        )
        open_now.append(d)
    result = ValidationResult(
        issues=Counter(d.code for d in open_now), verdict=verdict([d.severity for d in open_now])
    )
    # Automatic checks never verify a submission on their own; they route it.
    if result.verdict != "clear" and submission.status == SubmissionStatus.SUBMITTED:
        submission.status = SubmissionStatus.NEEDS_REVIEW
    db.flush()
    return result
