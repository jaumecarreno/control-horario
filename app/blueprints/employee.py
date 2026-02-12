"""Employee self-service routes."""

from __future__ import annotations

from datetime import datetime, time, timezone
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import select

from app.audit import log_audit
from app.extensions import db
from app.forms import LeaveRequestForm
from app.models import Employee, LeaveRequest, LeaveRequestStatus, LeaveType, TimeEvent, TimeEventSource, TimeEventType
from app.tenant import current_membership, tenant_required


bp = Blueprint("employee", __name__)

ACTION_MAP = {
    "in": TimeEventType.IN,
    "out": TimeEventType.OUT,
    "break-start": TimeEventType.BREAK_START,
    "break-end": TimeEventType.BREAK_END,
}


PUNCH_BUTTONS = [
    {"slug": "in", "label": "Registrar entrada", "icon": "->]", "class": "in"},
    {"slug": "out", "label": "Registrar salida", "icon": "[->", "class": "out"},
]


def _today_bounds_utc() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    end = datetime.combine(now.date(), time.max, tzinfo=timezone.utc)
    return start, end


def _employee_for_current_user() -> Employee:
    membership = current_membership()
    if membership is None or membership.employee_id is None:
        abort(403, description="No employee profile linked to this account.")

    employee = db.session.get(Employee, membership.employee_id)
    if employee is None or not employee.active:
        abort(403, description="Employee profile is unavailable.")
    return employee


def _todays_events(employee_id: uuid.UUID) -> list[TimeEvent]:
    start, end = _today_bounds_utc()
    stmt = (
        select(TimeEvent)
        .where(TimeEvent.employee_id == employee_id, TimeEvent.ts >= start, TimeEvent.ts <= end)
        .order_by(TimeEvent.ts.asc())
    )
    return list(db.session.execute(stmt).scalars().all())


def _current_presence_state(events: list[TimeEvent]) -> str:
    if not events:
        return "SALIDA"

    last_type = events[-1].type
    if last_type == TimeEventType.IN:
        return "ENTRADA"
    if last_type == TimeEventType.OUT:
        return "SALIDA"

    # Break events keep the most recent in/out state.
    for event in reversed(events[:-1]):
        if event.type == TimeEventType.IN:
            return "ENTRADA"
        if event.type == TimeEventType.OUT:
            return "SALIDA"
    return "SALIDA"


def _recent_punches(events: list[TimeEvent]) -> list[dict[str, str]]:
    rows = []
    for event in reversed(events):
        if event.type == TimeEventType.IN:
            rows.append({"label": "Entrada", "ts": event.ts})
        elif event.type == TimeEventType.OUT:
            rows.append({"label": "Salida", "ts": event.ts})
        if len(rows) == 5:
            break
    return rows


def _render_punch_state(employee: Employee):
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    return render_template(
        "employee/_punch_state.html",
        employee=employee,
        events=events,
        last_event=events[-1] if events else None,
        punch_buttons=PUNCH_BUTTONS,
        current_state=current_state,
        recent_punches=_recent_punches(events),
    )


@bp.get("/me/today")
@login_required
@tenant_required
def me_today():
    employee = _employee_for_current_user()
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    return render_template(
        "employee/today.html",
        employee=employee,
        events=events,
        last_event=events[-1] if events else None,
        punch_buttons=PUNCH_BUTTONS,
        current_state=current_state,
        recent_punches=_recent_punches(events),
    )


@bp.post("/me/punch/<string:action>")
@login_required
@tenant_required
def punch_action(action: str):
    event_type = ACTION_MAP.get(action)
    if event_type is None:
        abort(404)

    employee = _employee_for_current_user()
    event = TimeEvent(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        type=event_type,
        source=TimeEventSource.WEB,
        meta_json={"via": "employee_ui"},
    )
    db.session.add(event)
    db.session.flush()
    log_audit(
        action=f"PUNCH_{event_type.value}",
        entity_type="time_events",
        entity_id=event.id,
        payload={"employee_id": str(employee.id), "source": "WEB"},
    )
    db.session.commit()

    if request.headers.get("HX-Request") == "true":
        return _render_punch_state(employee)

    flash("Event recorded.", "success")
    return redirect(url_for("employee.me_today"))


@bp.get("/me/events")
@login_required
@tenant_required
def me_events():
    employee = _employee_for_current_user()
    stmt = select(TimeEvent).where(TimeEvent.employee_id == employee.id).order_by(TimeEvent.ts.desc()).limit(100)
    events = list(db.session.execute(stmt).scalars().all())
    return render_template("employee/events.html", employee=employee, events=events)


@bp.route("/me/leaves", methods=["GET", "POST"])
@login_required
@tenant_required
def me_leaves():
    employee = _employee_for_current_user()
    form = LeaveRequestForm()

    leave_types = list(
        db.session.execute(select(LeaveType).where(LeaveType.tenant_id == employee.tenant_id).order_by(LeaveType.name.asc()))
        .scalars()
        .all()
    )
    form.type_id.choices = [(str(item.id), f"{item.code} - {item.name}") for item in leave_types]
    leave_type_ids = {str(item.id): item for item in leave_types}

    if form.validate_on_submit():
        selected_type = leave_type_ids.get(form.type_id.data)
        if selected_type is None:
            abort(400, description="Invalid leave type.")

        leave_request = LeaveRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type_id=selected_type.id,
            date_from=form.date_from.data,
            date_to=form.date_to.data,
            minutes=form.minutes.data,
            status=LeaveRequestStatus.REQUESTED,
        )
        db.session.add(leave_request)
        db.session.flush()
        log_audit(
            action="LEAVE_REQUESTED",
            entity_type="leave_requests",
            entity_id=leave_request.id,
            payload={
                "employee_id": str(employee.id),
                "type_id": str(selected_type.id),
                "date_from": form.date_from.data.isoformat(),
                "date_to": form.date_to.data.isoformat(),
            },
        )
        db.session.commit()
        flash("Leave request submitted.", "success")
        return redirect(url_for("employee.me_leaves"))

    history_stmt = (
        select(LeaveRequest, LeaveType)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(LeaveRequest.employee_id == employee.id)
        .order_by(LeaveRequest.created_at.desc())
    )
    requests_rows = db.session.execute(history_stmt).all()
    return render_template("employee/leaves.html", form=form, rows=requests_rows, employee=employee)
