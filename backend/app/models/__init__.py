"""All ORM models. Importing this package registers every table on Base.metadata."""

from app.models.applicants import Applicant, Submission
from app.models.documents import Document, DocumentPage, ExtractedField, StoredFile
from app.models.registry import DocumentType, RequirementItem, RequirementSet
from app.models.review import AuditEvent, ValidationIssue
from app.models.tenancy import Institution, User

__all__ = [
    "Applicant",
    "AuditEvent",
    "Document",
    "DocumentPage",
    "DocumentType",
    "ExtractedField",
    "Institution",
    "RequirementItem",
    "RequirementSet",
    "StoredFile",
    "Submission",
    "User",
    "ValidationIssue",
]
