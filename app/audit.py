"""Audit logging helper."""

from __future__ import annotations

import uuid
from typing import Any

from flask import session
from flask_login import current_user

from app.extensions import db
from app.models import AuditLog


def log_audit(
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
) -> None:
    tenant_id = session.get("active_tenant_id")
    if not tenant_id:
        return
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except ValueError:
        return

    actor_user_id: uuid.UUID | None = None
    if current_user.is_authenticated:
        try:
            actor_user_id = uuid.UUID(current_user.get_id())
        except ValueError:
            actor_user_id = None

    db.session.add(
        AuditLog(
            tenant_id=tenant_uuid,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload_json=payload or {},
        )
    )
