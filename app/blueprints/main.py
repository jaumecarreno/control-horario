"""General routes."""

from __future__ import annotations

from flask import Blueprint, redirect, session, url_for
from flask_login import current_user


bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if not session.get("active_tenant_id"):
        return redirect(url_for("auth.select_tenant"))
    return redirect(url_for("employee.me_today"))


@bp.get("/health")
def health():
    return {"status": "ok"}, 200

