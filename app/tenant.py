"""Tenant and authorization helpers."""

from __future__ import annotations

import functools
import uuid
from typing import Callable

from flask import abort, g, session
from flask_login import current_user
from sqlalchemy import select

from app.extensions import db
from app.models import Membership, MembershipRole


def get_active_tenant_id() -> uuid.UUID | None:
    tenant_id = session.get("active_tenant_id")
    if not tenant_id:
        return None
    try:
        return uuid.UUID(str(tenant_id))
    except ValueError:
        session.pop("active_tenant_id", None)
        return None


def tenant_required(view: Callable):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("tenant_id"):
            abort(403, description="Tenant not selected.")
        return view(*args, **kwargs)

    return wrapped


def current_membership() -> Membership | None:
    if not current_user.is_authenticated:
        return None

    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        return None
    try:
        user_id = uuid.UUID(current_user.get_id())
    except ValueError:
        return None

    stmt = select(Membership).where(
        Membership.user_id == user_id,
        Membership.tenant_id == tenant_id,
    )
    return db.session.execute(stmt).scalar_one_or_none()


def landing_endpoint_for_membership(membership: Membership | None) -> str:
    if membership is None:
        return "auth.select_tenant"

    if membership.employee_id is not None:
        return "employee.me_today"

    if membership.role in {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}:
        return "admin.team_today"

    return "auth.select_tenant"


def roles_required(allowed_roles: set[MembershipRole]):
    def decorator(view: Callable):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            membership = current_membership()
            if membership is None or membership.role not in allowed_roles:
                abort(403, description="Insufficient permissions.")
            return view(*args, **kwargs)

        return wrapped

    return decorator
