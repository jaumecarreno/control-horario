"""Kiosk PIN authentication and punching."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select

from app.audit import log_audit
from app.extensions import db
from app.models import Employee, TimeEvent, TimeEventSource, TimeEventType
from app.security import verify_secret
from app.tenant import get_active_tenant_id, tenant_required


bp = Blueprint("kiosk", __name__)

ACTION_MAP = {
    "in": TimeEventType.IN,
    "out": TimeEventType.OUT,
    "break-start": TimeEventType.BREAK_START,
    "break-end": TimeEventType.BREAK_END,
}


def _current_kiosk_employee() -> Employee | None:
    employee_id = session.get("kiosk_employee_id")
    if not employee_id:
        return None
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        return None
    employee = db.session.get(Employee, employee_id)
    if employee is None or not employee.active or employee.tenant_id != tenant_id:
        session.pop("kiosk_employee_id", None)
        return None
    return employee


def _render_kiosk_panel(employee: Employee | None):
    return render_template("kiosk/_panel.html", employee=employee)


@bp.get("/kiosk")
@tenant_required
def kiosk_index():
    employee = _current_kiosk_employee()
    return render_template("kiosk/index.html", employee=employee)


@bp.post("/kiosk/auth-pin")
@tenant_required
def kiosk_auth_pin():
    raw_pin = request.form.get("pin", "").strip()
    if len(raw_pin) < 4:
        abort(400, description="PIN must contain at least 4 characters.")

    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(403, description="Tenant not selected.")

    candidates = db.session.execute(
        select(Employee).where(
            Employee.tenant_id == tenant_id,
            Employee.active.is_(True),
            Employee.pin_hash.is_not(None),
        )
    ).scalars()

    employee = None
    for item in candidates:
        if item.pin_hash and verify_secret(item.pin_hash, raw_pin):
            employee = item
            break

    if employee is None:
        if request.headers.get("HX-Request") == "true":
            return render_template("kiosk/_panel.html", employee=None, pin_error="Invalid PIN."), 401
        flash("Invalid PIN.", "danger")
        return redirect(url_for("kiosk.kiosk_index"))

    session["kiosk_employee_id"] = str(employee.id)
    if request.headers.get("HX-Request") == "true":
        return _render_kiosk_panel(employee)
    return redirect(url_for("kiosk.kiosk_index"))


@bp.post("/kiosk/punch/<string:action>")
@tenant_required
def kiosk_punch(action: str):
    event_type = ACTION_MAP.get(action)
    if event_type is None:
        abort(404)

    employee = _current_kiosk_employee()
    if employee is None:
        abort(403, description="Authenticate PIN first.")

    event = TimeEvent(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        type=event_type,
        source=TimeEventSource.KIOSK,
        meta_json={"via": "kiosk"},
    )
    db.session.add(event)
    db.session.flush()
    log_audit(
        action=f"KIOSK_PUNCH_{event_type.value}",
        entity_type="time_events",
        entity_id=event.id,
        payload={"employee_id": str(employee.id), "source": "KIOSK"},
    )
    db.session.commit()

    if request.headers.get("HX-Request") == "true":
        return _render_kiosk_panel(employee)
    flash("Event recorded.", "success")
    return redirect(url_for("kiosk.kiosk_index"))
