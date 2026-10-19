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
