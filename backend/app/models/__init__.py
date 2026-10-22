"""All ORM models. Importing this package registers every table on Base.metadata."""

from app.models.applicants import Applicant, Submission, SubmissionMessage
from app.models.documents import Document, DocumentPage, ExtractedField, StoredFile
from app.models.imports import ImportFileRef
from app.models.jobs import Job
from app.models.learning import MlModel
from app.models.ocr import PageOcr
from app.models.registry import DocumentType, RequirementItem, RequirementSet
from app.models.review import AuditEvent, ValidationIssue
from app.models.tenancy import Institution, LoginCode, User, UserSession

__all__ = [
    "Applicant",
    "AuditEvent",
    "Document",
    "DocumentPage",
    "DocumentType",
    "ExtractedField",
    "ImportFileRef",
    "Job",
    "LoginCode",
    "MlModel",
    "Institution",
    "PageOcr",
    "RequirementItem",
    "RequirementSet",
    "StoredFile",
    "Submission",
    "SubmissionMessage",
    "User",
    "UserSession",
    "ValidationIssue",
]
