"""Enumerations shared by the domain models. Stored as strings in the database."""

from enum import StrEnum


class UserRole(StrEnum):
    STUDENT = "student"
    VERIFIER = "verifier"
    INSTITUTION_ADMIN = "institution_admin"
    PLATFORM_ADMIN = "platform_admin"


class DocumentCategory(StrEnum):
    ACADEMIC_RECORD = "academic_record"  # marksheets, certificates: OCR + extraction
    IDENTITY_IMAGE = "identity_image"  # photo, signature: stored and displayed, not OCR'd
    SUPPORTING = "supporting"  # back sides, instruction pages: kept, not verified


class ApplicantSource(StrEnum):
    FORM_IMPORT = "form_import"  # seed data from the Google Form export
    PORTAL = "portal"  # uploaded directly on the platform


class SubmissionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class QualityVerdict(StrEnum):
    OK = "ok"
    WARN = "warn"  # usable, but a reviewer should know (e.g. low resolution)
    RETAKE = "retake"  # too poor to read reliably; ask the student for a better copy
    BLANK = "blank"  # no content (e.g. back side of a certificate)


class OcrStatus(StrEnum):
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"  # read, but a reviewer should check it
    NO_TEXT = "no_text"  # nothing readable found
    FAILED = "failed"  # engine error


class ClassificationMethod(StrEnum):
    CONTENT_EXACT = "content_exact"  # an exact title/keyword pattern matched
    CONTENT_FUZZY = "content_fuzzy"  # an OCR-damaged title matched approximately
    CONTINUATION = "continuation"  # unreadable later page: same type as the page before
    SLOT_HINT = "slot_hint"  # unreadable page in a slot that accepts only one type


class AcademicLevel(StrEnum):
    SCHOOL = "school"
    UG = "ug"
    PG = "pg"


class FieldSource(StrEnum):
    MACHINE = "machine"
    HUMAN = "human"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ImportFileStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    FAILED = "failed"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobStage(StrEnum):
    """Where a document is in the processing pipeline (shown to the uploader)."""

    WAITING = "waiting"
    QUALITY = "quality"  # opening the file, rendering pages, quality checks
    READING = "reading"  # OCR
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    DONE = "done"
