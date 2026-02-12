"""Employee self-service routes."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
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
    {"slug": "in", "label": "Registrar ENTRADA", "class": "in"},
    {"slug": "out", "label": "Registrar SALIDA", "class": "out"},
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
        is_manual = bool((event.meta_json or {}).get("manual"))
        if event.type == TimeEventType.IN:
            rows.append({"label": "Entrada", "ts": event.ts.strftime("%H:%M:%S"), "manual": " · Manual" if is_manual else ""})
        elif event.type == TimeEventType.OUT:
            rows.append({"label": "Salida", "ts": event.ts.strftime("%H:%M:%S"), "manual": " · Manual" if is_manual else ""})
        if len(rows) == 5:
            break
    return rows


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year=year, month=month, day=1, tzinfo=timezone.utc)
    days_in_month = monthrange(year, month)[1]
    end = datetime(year=year, month=month, day=days_in_month, hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
    return start, end


def _daily_worked_minutes(events: list[TimeEvent]) -> tuple[int, list[str], bool]:
    entries_and_exits: list[str] = []
    worked_minutes = 0
    open_entry: TimeEvent | None = None
    includes_manual = False

    for event in events:
        if event.type != TimeEventType.IN and event.type != TimeEventType.OUT:
            continue

        if event.type == TimeEventType.IN:
            open_entry = event
            continue

        if open_entry is None:
            continue

        delta = event.ts - open_entry.ts
        worked_minutes += max(0, int(delta.total_seconds() // 60))
        pair_has_manual = bool((open_entry.meta_json or {}).get("manual")) or bool((event.meta_json or {}).get("manual"))
        if pair_has_manual:
            includes_manual = True
        pair_label = f"{open_entry.ts.strftime('%H:%M')} → {event.ts.strftime('%H:%M')}"
        if pair_has_manual:
            pair_label += " (Manual)"
        entries_and_exits.append(pair_label)
        open_entry = None

    return worked_minutes, entries_and_exits, includes_manual


def _minutes_to_hhmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    total = abs(minutes)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


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
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    requested_state = "ENTRADA" if action == "in" else "SALIDA" if action == "out" else None
    allow_repeat = request.form.get("confirm_repeat") == "1"

    if requested_state == current_state and not allow_repeat:
        if request.headers.get("HX-Request") == "true":
            return _render_punch_state(employee)

        flash("Marcaje cancelado.", "info")
        return redirect(url_for("employee.me_today"))

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


@bp.post("/me/incidents/manual")
@login_required
@tenant_required
def create_manual_punch():
    employee = _employee_for_current_user()
    requested_date = request.form.get("manual_date", "")
    requested_hour = request.form.get("manual_hour", "")
    requested_minute = request.form.get("manual_minute", "")
    requested_kind = request.form.get("manual_kind", "")

    try:
        event_date = date.fromisoformat(requested_date)
        hour = int(requested_hour)
        minute = int(requested_minute)
    except (TypeError, ValueError):
        flash("Fecha u hora inválida para el fichaje manual.", "danger")
        return redirect(url_for("employee.me_today"))

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        flash("La hora manual debe estar entre 00:00 y 23:59.", "danger")
        return redirect(url_for("employee.me_today"))

    event_type = TimeEventType.IN if requested_kind == "IN" else TimeEventType.OUT if requested_kind == "OUT" else None
    if event_type is None:
        flash("Tipo de fichaje manual inválido.", "danger")
        return redirect(url_for("employee.me_today"))

    event_ts = datetime.combine(event_date, time(hour=hour, minute=minute), tzinfo=timezone.utc)
    event = TimeEvent(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        type=event_type,
        source=TimeEventSource.WEB,
        ts=event_ts,
        meta_json={"via": "employee_manual_incident", "manual": True},
    )
    db.session.add(event)
    db.session.flush()
    log_audit(
        action=f"PUNCH_{event_type.value}_MANUAL",
        entity_type="time_events",
        entity_id=event.id,
        payload={"employee_id": str(employee.id), "source": "WEB", "manual": True},
    )
    db.session.commit()

    flash("Fichaje manual registrado correctamente.", "success")
    return redirect(url_for("employee.presence_control"))


@bp.get("/me/presence-control")
@login_required
@tenant_required
def presence_control():
    employee = _employee_for_current_user()
    requested_month = request.args.get("month")

    if requested_month:
        try:
            selected_year, selected_month = map(int, requested_month.split("-", 1))
        except ValueError:
            selected_year = datetime.now(timezone.utc).year
            selected_month = datetime.now(timezone.utc).month
    else:
        now = datetime.now(timezone.utc)
        selected_year = now.year
        selected_month = now.month

    month_start, month_end = _month_bounds_utc(selected_year, selected_month)
    month_events_stmt = (
        select(TimeEvent)
        .where(TimeEvent.employee_id == employee.id, TimeEvent.ts >= month_start, TimeEvent.ts <= month_end)
        .order_by(TimeEvent.ts.asc())
    )
    month_events = list(db.session.execute(month_events_stmt).scalars().all())
    events_by_day: dict[date, list[TimeEvent]] = {}
    for event in month_events:
        events_by_day.setdefault(event.ts.date(), []).append(event)

    month_rows = []
    month_worked = 0
    month_expected = 0
    for day_index in range(monthrange(selected_year, selected_month)[1]):
        current_day = date(selected_year, selected_month, day_index + 1)
        day_events = events_by_day.get(current_day, [])
        worked_minutes, day_pairs, includes_manual = _daily_worked_minutes(day_events)
        expected_minutes = 450 if current_day.weekday() < 5 else 0
        month_worked += worked_minutes
        month_expected += expected_minutes
        month_rows.append(
            {
                "day": current_day,
                "pairs": day_pairs,
                "worked": worked_minutes,
                "expected": expected_minutes,
                "balance": worked_minutes - expected_minutes,
                "has_manual": includes_manual,
            }
        )

    recent_stmt = select(TimeEvent).where(TimeEvent.employee_id == employee.id).order_by(TimeEvent.ts.desc()).limit(12)
    recent_events = list(db.session.execute(recent_stmt).scalars().all())
    prev_month = (month_start - timedelta(days=1)).strftime("%Y-%m")
    next_month = (month_end + timedelta(days=1)).strftime("%Y-%m")

    return render_template(
        "employee/presence_control.html",
        employee=employee,
        month_rows=month_rows,
        selected_month=f"{selected_year:04d}-{selected_month:02d}",
        month_label=month_start.strftime("%B %Y"),
        month_worked=_minutes_to_hhmm(month_worked),
        month_expected=_minutes_to_hhmm(month_expected),
        month_balance=_minutes_to_hhmm(month_worked - month_expected),
        recent_events=recent_events,
        prev_month=prev_month,
        next_month=next_month,
        minutes_to_hhmm=_minutes_to_hhmm,
    )


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
