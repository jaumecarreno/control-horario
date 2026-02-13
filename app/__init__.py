"""Flask application factory."""

from __future__ import annotations

import uuid

from flask import Flask, g, redirect, request, session, url_for
from flask_login import current_user

from app.blueprints.admin import bp as admin_bp
from app.blueprints.auth import bp as auth_bp
from app.blueprints.employee import bp as employee_bp
from app.blueprints.main import bp as main_bp
from app.config import Config
from app.extensions import csrf, db, init_rls_session_listener, login_manager


TENANT_OPTIONAL_ENDPOINTS = {
    "auth.login",
    "auth.logout",
    "auth.select_tenant",
    "main.health",
    "main.index",
    "static",
}

TENANT_REQUIRED_PREFIXES = ("employee.", "admin.")
NO_TENANT_UUID = "00000000-0000-0000-0000-000000000000"


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    init_rls_session_listener()

    # Ensure model metadata is loaded for migrations and tests.
    from app import models as _models  # noqa: F401

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(employee_bp)
    app.register_blueprint(admin_bp)

    @app.context_processor
    def inject_nav_profile() -> dict[str, str]:
        if not current_user.is_authenticated:
            return {}

        from app.models import Employee
        from app.tenant import current_membership

        membership = current_membership()
        profile_name = ""
        profile_role = "USER"

        if membership is not None:
            profile_role = getattr(membership.role, "value", str(membership.role))
            if membership.employee_id is not None:
                employee = db.session.get(Employee, membership.employee_id)
                if employee is not None and employee.name:
                    profile_name = employee.name.strip()

        if not profile_name:
            email = getattr(current_user, "email", "") or ""
            profile_name = email.split("@", 1)[0] if email else "Usuario"
            profile_name = profile_name.strip() or "Usuario"

        name_parts = [chunk for chunk in profile_name.split() if chunk]
        if len(name_parts) >= 2:
            profile_initials = (name_parts[0][0] + name_parts[1][0]).upper()
        else:
            profile_initials = profile_name[:2].upper()
        profile_initials = profile_initials or "U"

        return {
            "nav_profile_name": profile_name,
            "nav_profile_role": profile_role,
            "nav_profile_initials": profile_initials,
        }

    @app.before_request
    def load_request_db_context() -> None:
        db.session.info.pop("tenant_id", None)
        db.session.info.pop("actor_user_id", None)
        g.tenant_id = None

        if current_user.is_authenticated:
            db.session.info["actor_user_id"] = current_user.get_id()
            db.session.info["tenant_id"] = NO_TENANT_UUID

        tenant_id = session.get("active_tenant_id")
        if tenant_id:
            try:
                tenant_uuid = uuid.UUID(str(tenant_id))
            except ValueError:
                session.pop("active_tenant_id", None)
            else:
                g.tenant_id = str(tenant_uuid)
                db.session.info["tenant_id"] = str(tenant_uuid)

    @app.before_request
    def enforce_tenant_selection() -> str | None:
        endpoint = request.endpoint or ""
        if endpoint.startswith("static"):
            return None

        if endpoint in TENANT_OPTIONAL_ENDPOINTS:
            return None

        needs_tenant = endpoint.startswith(TENANT_REQUIRED_PREFIXES)
        if needs_tenant and not session.get("active_tenant_id"):
            if current_user.is_authenticated:
                return redirect(url_for("auth.select_tenant"))
            return None
        return None

    @app.teardown_request
    def cleanup_session_context(_exc: BaseException | None) -> None:
        db.session.info.pop("tenant_id", None)
        db.session.info.pop("actor_user_id", None)

    return app
