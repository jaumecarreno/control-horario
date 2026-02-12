"""Authentication and tenant selection routes."""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from app.extensions import db
from app.forms import LoginForm, TenantSelectForm
from app.models import Membership, Tenant, User
from app.security import verify_secret


bp = Blueprint("auth", __name__)
NO_TENANT_UUID = "00000000-0000-0000-0000-000000000000"


def _is_safe_next(target: str) -> bool:
    if not target:
        return False
    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == ""


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        if session.get("active_tenant_id"):
            return redirect(url_for("employee.me_today"))
        return redirect(url_for("auth.select_tenant"))

    form = LoginForm()
    if form.validate_on_submit():
        stmt = select(User).where(User.email == form.email.data.strip().lower())
        user = db.session.execute(stmt).scalar_one_or_none()
        if user is None or not verify_secret(user.password_hash, form.password.data):
            flash("Invalid credentials.", "danger")
            return render_template("auth/login.html", form=form), 401

        if not user.is_active:
            flash("User is inactive.", "warning")
            return render_template("auth/login.html", form=form), 403

        login_user(user, remember=form.remember.data)
        session.pop("active_tenant_id", None)

        db.session.info["actor_user_id"] = str(user.id)
        db.session.info["tenant_id"] = NO_TENANT_UUID
        db.session.rollback()
        memberships = db.session.execute(select(Membership).where(Membership.user_id == user.id)).scalars().all()
        if len(memberships) == 1:
            session["active_tenant_id"] = str(memberships[0].tenant_id)
            return redirect(url_for("employee.me_today"))

        next_url = request.args.get("next")
        if _is_safe_next(next_url):
            return redirect(next_url)
        return redirect(url_for("auth.select_tenant"))

    return render_template("auth/login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    session.pop("active_tenant_id", None)
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/select-tenant", methods=["GET", "POST"])
@login_required
def select_tenant():
    form = TenantSelectForm()
    memberships_stmt = (
        select(Membership, Tenant)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(Membership.user_id == current_user.id)
        .order_by(Tenant.name.asc())
    )
    membership_rows = db.session.execute(memberships_stmt).all()
    if not membership_rows:
        flash("No tenant memberships found for this user.", "warning")
        return render_template("auth/select_tenant.html", form=form, memberships=[])

    form.tenant_id.choices = [(str(membership.tenant_id), tenant.name) for membership, tenant in membership_rows]

    if request.method == "GET" and len(membership_rows) == 1:
        session["active_tenant_id"] = str(membership_rows[0][0].tenant_id)
        return redirect(url_for("employee.me_today"))

    if form.validate_on_submit():
        allowed_tenant_ids = {str(membership.tenant_id) for membership, _ in membership_rows}
        if form.tenant_id.data not in allowed_tenant_ids:
            flash("Invalid tenant choice.", "danger")
            return render_template("auth/select_tenant.html", form=form, memberships=membership_rows), 403

        session["active_tenant_id"] = form.tenant_id.data
        return redirect(url_for("employee.me_today"))

    return render_template("auth/select_tenant.html", form=form, memberships=membership_rows)
