"""Flask extension instances and shared listeners."""

from __future__ import annotations

import uuid

from flask import redirect, request, url_for
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session as OrmSession


db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

_rls_listener_registered = False


@login_manager.unauthorized_handler
def handle_unauthorized() -> str:
    return redirect(url_for("auth.login", next=request.path))


def _safe_uuid(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def init_rls_session_listener() -> None:
    """Set tenant/actor PostgreSQL custom settings at transaction start."""
    global _rls_listener_registered
    if _rls_listener_registered:
        return

    @event.listens_for(OrmSession, "after_begin")
    def set_rls_context(session: OrmSession, _transaction: object, connection: Connection) -> None:
        if connection.dialect.name != "postgresql":
            return

        tenant_id = _safe_uuid(session.info.get("tenant_id"))
        actor_user_id = _safe_uuid(session.info.get("actor_user_id"))

        if tenant_id:
            connection.exec_driver_sql(f"SET LOCAL app.tenant_id = '{tenant_id}'")
        if actor_user_id:
            connection.exec_driver_sql(f"SET LOCAL app.actor_user_id = '{actor_user_id}'")

    _rls_listener_registered = True


@login_manager.user_loader
def load_user(user_id: str):
    from app.models import User

    try:
        parsed = uuid.UUID(user_id)
    except ValueError:
        return None
    return db.session.get(User, parsed)
