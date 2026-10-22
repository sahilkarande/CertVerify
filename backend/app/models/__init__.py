"""All ORM models. Importing this package registers every table on Base.metadata."""

from app.models.applicants import Applicant, Submission
from app.models.documents import Document, DocumentPage, ExtractedField, StoredFile
from app.models.imports import ImportFileRef
from app.models.ocr import PageOcr
from app.models.registry import DocumentType, RequirementItem, RequirementSet
from app.models.review import AuditEvent, ValidationIssue
from app.models.tenancy import Institution, User, UserSession

__all__ = [
    "Applicant",
    "AuditEvent",
    "Document",
    "DocumentPage",
    "DocumentType",
    "ExtractedField",
    "ImportFileRef",
    "Institution",
    "PageOcr",
    "RequirementItem",
    "RequirementSet",
    "StoredFile",
    "Submission",
    "User",
    "UserSession",
    "ValidationIssue",
]
