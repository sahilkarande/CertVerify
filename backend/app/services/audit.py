"""Audit trail helper."""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record_event(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    actor_user_id: uuid.UUID | None = None,
    institution_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add an audit event to the current transaction. Keep `details` free of PII."""
    event = AuditEvent(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        actor_user_id=actor_user_id,
        institution_id=institution_id,
        details=details or {},
    )
    db.add(event)
    return event
