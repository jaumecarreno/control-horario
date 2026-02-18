"""Employee self-service routes."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.audit import log_audit
from app.extensions import db
from app.forms import LeaveRequestForm
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Shift,
    ShiftLeavePolicy,
    TimeEvent,
    TimeEventSource,
    TimeEventType,
)
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

SHIFT_FREQUENCY_LABELS = {
    ExpectedHoursFrequency.YEARLY: "Anuales",
    ExpectedHoursFrequency.MONTHLY: "Mensuales",
    ExpectedHoursFrequency.WEEKLY: "Semanales",
    ExpectedHoursFrequency.DAILY: "Diarias",
}
LEAVE_POLICY_UNIT_LABELS = {
    LeavePolicyUnit.DAYS: "dias",
    LeavePolicyUnit.HOURS: "horas",
}
LEAVE_STATUS_LABELS = {
    LeaveRequestStatus.REQUESTED: "Pendiente",
    LeaveRequestStatus.APPROVED: "Aprobada",
    LeaveRequestStatus.REJECTED: "Rechazada",
    LeaveRequestStatus.CANCELLED: "Cancelada",
}


def _today_bounds_utc() -> tuple[datetime, datetime]:
    tz = _app_timezone()
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    end_local = datetime.combine(now_local.date(), time.max, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _app_timezone() -> ZoneInfo:
    tz_name = current_app.config.get("APP_TIMEZONE", "Europe/Madrid")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _to_app_tz(ts: datetime) -> datetime:
    aware_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return aware_ts.astimezone(_app_timezone())


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
        event_local_ts = _to_app_tz(event.ts)
        if event.type == TimeEventType.IN:
            rows.append({"label": "Entrada", "ts": event_local_ts.strftime("%H:%M:%S"), "manual": " · Manual" if is_manual else ""})
        elif event.type == TimeEventType.OUT:
            rows.append({"label": "Salida", "ts": event_local_ts.strftime("%H:%M:%S"), "manual": " · Manual" if is_manual else ""})
        if len(rows) == 5:
            break
    return rows


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    tz = _app_timezone()
    start = datetime(year=year, month=month, day=1, tzinfo=tz)
    days_in_month = monthrange(year, month)[1]
    end = datetime(year=year, month=month, day=days_in_month, hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _tenant_shift(tenant_id: uuid.UUID) -> Shift | None:
    stmt = select(Shift).where(Shift.tenant_id == tenant_id).order_by(Shift.created_at.asc(), Shift.name.asc()).limit(1)
    try:
        return db.session.execute(stmt).scalar_one_or_none()
    except (OperationalError, ProgrammingError, LookupError):
        # Keep presence/pause control working with default values if shifts schema/data is inconsistent.
        db.session.rollback()
        current_app.logger.warning(
            "Shift lookup failed. Falling back to default attendance settings. "
            "Run `alembic upgrade head` to apply pending migrations.",
            exc_info=True,
        )
        return None


def _employee_shift_assignments(
    employee_id: uuid.UUID,
    start_day: date,
    end_day: date,
) -> list[tuple[EmployeeShiftAssignment, Shift | None]] | None:
    stmt = (
        select(EmployeeShiftAssignment, Shift)
        .outerjoin(Shift, Shift.id == EmployeeShiftAssignment.shift_id)
        .where(
            EmployeeShiftAssignment.employee_id == employee_id,
            EmployeeShiftAssignment.effective_from <= end_day,
            or_(EmployeeShiftAssignment.effective_to.is_(None), EmployeeShiftAssignment.effective_to >= start_day),
        )
        .order_by(EmployeeShiftAssignment.effective_from.asc(), EmployeeShiftAssignment.created_at.asc())
    )
    try:
        return list(db.session.execute(stmt).all())
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Employee shift assignment lookup failed. Falling back to tenant-level shift. "
            "Run `alembic upgrade head` to apply pending migrations.",
            exc_info=True,
        )
        return None


def _shift_for_day(
    assignment_rows: list[tuple[EmployeeShiftAssignment, Shift | None]],
    current_day: date,
) -> Shift | None:
    for assignment, shift in reversed(assignment_rows):
        if assignment.effective_from <= current_day and (assignment.effective_to is None or assignment.effective_to >= current_day):
            return shift
    return None


def _current_shift_for_employee_day(employee: Employee, current_day: date) -> Shift | None:
    assignment_rows = _employee_shift_assignments(employee.id, current_day, current_day)
    if assignment_rows is None:
        return _tenant_shift(employee.tenant_id)
    return _shift_for_day(assignment_rows, current_day)


def _format_decimal_amount(value: Decimal) -> str:
    value_as_float = float(value)
    if abs(value_as_float - round(value_as_float)) < 0.000001:
        return str(int(round(value_as_float)))
    return f"{value_as_float:.2f}".rstrip("0").rstrip(".")


def _leave_request_amount_for_policy(policy: ShiftLeavePolicy, leave_request: LeaveRequest) -> Decimal:
    if policy.unit == LeavePolicyUnit.DAYS:
        return Decimal(max(0, (leave_request.date_to - leave_request.date_from).days + 1))
    minutes = max(0, int(leave_request.minutes or 0))
    return Decimal(minutes) / Decimal(60)


def _policy_consumption_by_employee(
    employee_id: uuid.UUID,
    policies: list[ShiftLeavePolicy],
) -> dict[uuid.UUID, dict[str, Decimal]]:
    totals: dict[uuid.UUID, dict[str, Decimal]] = {
        policy.id: {"approved": Decimal("0"), "pending": Decimal("0")} for policy in policies
    }
    if not policies:
        return totals

    policy_by_id = {policy.id: policy for policy in policies}
    stmt = (
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_policy_id.in_(list(policy_by_id.keys())),
            LeaveRequest.status.in_([LeaveRequestStatus.REQUESTED, LeaveRequestStatus.APPROVED]),
        )
        .order_by(LeaveRequest.created_at.asc())
    )
    try:
        rows = list(db.session.execute(stmt).scalars().all())
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Leave request policy consumption lookup failed.",
            exc_info=True,
        )
        return totals

    for leave_request in rows:
        if leave_request.leave_policy_id is None:
            continue
        policy = policy_by_id.get(leave_request.leave_policy_id)
        if policy is None:
            continue
        amount = _leave_request_amount_for_policy(policy, leave_request)
        if leave_request.status == LeaveRequestStatus.APPROVED:
            totals[policy.id]["approved"] += amount
        elif leave_request.status == LeaveRequestStatus.REQUESTED:
            totals[policy.id]["pending"] += amount
    return totals


def _active_leave_policies_for_shift(shift: Shift | None, current_day: date) -> list[ShiftLeavePolicy]:
    if shift is None:
        return []

    stmt = (
        select(ShiftLeavePolicy)
        .where(
            ShiftLeavePolicy.tenant_id == shift.tenant_id,
            ShiftLeavePolicy.shift_id == shift.id,
            ShiftLeavePolicy.valid_from <= current_day,
            ShiftLeavePolicy.valid_to >= current_day,
        )
        .order_by(ShiftLeavePolicy.name.asc(), ShiftLeavePolicy.created_at.asc())
    )
    try:
        return list(db.session.execute(stmt).scalars().all())
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Shift leave policies lookup failed.",
            exc_info=True,
        )
        return []


def _leave_policy_balances(employee: Employee, shift: Shift | None, current_day: date) -> list[dict[str, str | float]]:
    policies = _active_leave_policies_for_shift(shift, current_day)
    if not policies:
        return []

    consumption_by_policy = _policy_consumption_by_employee(employee.id, policies)
    balances: list[dict[str, str | float]] = []
    for policy in policies:
        totals = consumption_by_policy.get(policy.id, {"approved": Decimal("0"), "pending": Decimal("0")})
        approved = totals["approved"]
        pending = totals["pending"]
        used = approved + pending
        total_amount = Decimal(policy.amount)
        remaining = total_amount - used
        if remaining < 0:
            remaining = Decimal("0")

        remaining_percent = 0.0
        if total_amount > 0:
            remaining_percent = float((remaining / total_amount) * Decimal("100"))
            if remaining_percent < 0:
                remaining_percent = 0.0
            if remaining_percent > 100:
                remaining_percent = 100.0

        balances.append(
            {
                "name": policy.name,
                "total_display": _format_decimal_amount(total_amount),
                "used_display": _format_decimal_amount(used),
                "pending_display": _format_decimal_amount(pending),
                "remaining_display": _format_decimal_amount(remaining),
                "unit_label": LEAVE_POLICY_UNIT_LABELS.get(policy.unit, "dias"),
                "remaining_percent": remaining_percent,
            }
        )

    return balances


def _work_balance_summary(employee: Employee, current_day: date) -> dict[str, str]:
    month_start_utc, _ = _month_bounds_utc(current_day.year, current_day.month)
    day_end_utc = datetime.combine(current_day, time.max, tzinfo=_app_timezone()).astimezone(timezone.utc)
    month_events_stmt = (
        select(TimeEvent)
        .where(
            TimeEvent.employee_id == employee.id,
            TimeEvent.ts >= month_start_utc,
            TimeEvent.ts <= day_end_utc,
        )
        .order_by(TimeEvent.ts.asc())
    )
    month_events = list(db.session.execute(month_events_stmt).scalars().all())
    events_by_day: dict[date, list[TimeEvent]] = {}
    for event in month_events:
        events_by_day.setdefault(_to_app_tz(event.ts).date(), []).append(event)

    month_start_day = date(current_day.year, current_day.month, 1)
    assignment_rows = _employee_shift_assignments(employee.id, month_start_day, current_day)
    fallback_shift: Shift | None = None
    if assignment_rows is None:
        assignment_rows = []
        fallback_shift = _tenant_shift(employee.tenant_id)

    business_days_in_month = _business_days_in_month(current_day.year, current_day.month)
    business_days_in_year = _business_days_in_year(current_day.year)
    total_balance_minutes = 0
    last_day_balance_minutes = 0
    last_day_label = "Sin fichajes"

    for day_index in range(current_day.day):
        row_day = date(current_day.year, current_day.month, day_index + 1)
        row_events = events_by_day.get(row_day, [])
        day_shift = fallback_shift if fallback_shift is not None else _shift_for_day(assignment_rows, row_day)
        worked_minutes, _, _ = _daily_worked_minutes(row_events)
        paused_minutes, _ = _daily_pause_minutes(row_events)
        if day_shift is not None and not day_shift.break_counts_as_worked_bool:
            worked_minutes = max(0, worked_minutes - paused_minutes)
        expected_minutes = _expected_work_minutes_for_day(
            day_shift,
            row_day,
            business_days_in_month,
            business_days_in_year,
        )

        day_balance = worked_minutes - expected_minutes
        total_balance_minutes += day_balance

        has_work_events = any(event.type in {TimeEventType.IN, TimeEventType.OUT} for event in row_events)
        if has_work_events:
            last_day_balance_minutes = day_balance
            last_day_label = row_day.strftime("%d/%m")

    return {
        "total_display": _minutes_to_hhmm(total_balance_minutes),
        "last_day_display": _minutes_to_hhmm(last_day_balance_minutes),
        "last_day_label": last_day_label,
    }


def _requested_amount_for_policy(
    policy: ShiftLeavePolicy,
    requested_from: date,
    requested_to: date,
    requested_minutes: int | None,
) -> tuple[Decimal | None, str | None]:
    if policy.unit == LeavePolicyUnit.DAYS:
        return Decimal(max(0, (requested_to - requested_from).days + 1)), None

    if requested_minutes is None or requested_minutes <= 0:
        return None, "Para permisos en horas debes indicar minutos mayores que cero."
    return Decimal(requested_minutes) / Decimal(60), None


def _has_leave_overlap(
    employee_id: uuid.UUID,
    leave_policy_id: uuid.UUID,
    requested_from: date,
    requested_to: date,
) -> bool:
    stmt = (
        select(LeaveRequest.id)
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_policy_id == leave_policy_id,
            LeaveRequest.status.in_([LeaveRequestStatus.REQUESTED, LeaveRequestStatus.APPROVED]),
            LeaveRequest.date_from <= requested_to,
            LeaveRequest.date_to >= requested_from,
        )
        .limit(1)
    )
    return db.session.execute(stmt).scalar_one_or_none() is not None


def _business_days_in_month(year: int, month: int) -> int:
    return sum(1 for day in range(1, monthrange(year, month)[1] + 1) if date(year, month, day).weekday() < 5)


def _business_days_in_year(year: int) -> int:
    total = 0
    for month in range(1, 13):
        total += _business_days_in_month(year, month)
    return total


def _expected_work_minutes_for_day(
    shift: Shift | None,
    current_day: date,
    business_days_in_month: int,
    business_days_in_year: int,
) -> int:
    if current_day.weekday() >= 5:
        return 0

    if shift is None:
        return 450

    total_minutes = max(0.0, float(shift.expected_hours) * 60.0)
    if shift.expected_hours_frequency == ExpectedHoursFrequency.DAILY:
        return int(round(total_minutes))
    if shift.expected_hours_frequency == ExpectedHoursFrequency.WEEKLY:
        return int(round(total_minutes / 5))
    if shift.expected_hours_frequency == ExpectedHoursFrequency.MONTHLY:
        return int(round(total_minutes / max(1, business_days_in_month)))
    return int(round(total_minutes / max(1, business_days_in_year)))


def _expected_pause_minutes_for_day(shift: Shift | None, current_day: date) -> int:
    if current_day.weekday() >= 5:
        return 0
    if shift is None:
        return 30
    return max(0, int(shift.break_minutes))


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
        pair_label = f"{_to_app_tz(open_entry.ts).strftime('%H:%M')} → {_to_app_tz(event.ts).strftime('%H:%M')}"
        if pair_has_manual:
            pair_label += " (Manual)"
        entries_and_exits.append(pair_label)
        open_entry = None

    return worked_minutes, entries_and_exits, includes_manual


def _daily_punch_markers(events: list[TimeEvent]) -> list[str]:
    markers: list[str] = []
    for event in events:
        if event.type != TimeEventType.IN and event.type != TimeEventType.OUT:
            continue
        event_label = "Entrada" if event.type == TimeEventType.IN else "Salida"
        marker = f"{event_label} {_to_app_tz(event.ts).strftime('%H:%M')}"
        if bool((event.meta_json or {}).get("manual")):
            marker += " (Manual)"
        markers.append(marker)
    return markers


def _daily_pause_minutes(events: list[TimeEvent]) -> tuple[int, list[str]]:
    pause_minutes = 0
    pause_pairs: list[str] = []
    open_pause: TimeEvent | None = None

    for event in events:
        if event.type == TimeEventType.BREAK_START:
            open_pause = event
            continue

        if event.type != TimeEventType.BREAK_END or open_pause is None:
            continue

        delta = event.ts - open_pause.ts
        pause_minutes += max(0, int(delta.total_seconds() // 60))
        pause_pairs.append(f"{_to_app_tz(open_pause.ts).strftime('%H:%M')} → {_to_app_tz(event.ts).strftime('%H:%M')}")
        open_pause = None

    return pause_minutes, pause_pairs


def _pause_summary(events: list[TimeEvent]) -> tuple[bool, int, int]:
    total_paused_seconds = 0
    open_pause: TimeEvent | None = None

    for event in events:
        if event.type == TimeEventType.BREAK_START:
            open_pause = event
            continue

        if event.type == TimeEventType.BREAK_END and open_pause is not None:
            total_paused_seconds += max(0, int((event.ts - open_pause.ts).total_seconds()))
            open_pause = None

    if open_pause is None:
        return False, 0, total_paused_seconds // 60

    pause_start = open_pause.ts if open_pause.ts.tzinfo else open_pause.ts.replace(tzinfo=timezone.utc)
    running_seconds = max(0, int((datetime.now(timezone.utc) - pause_start).total_seconds()))
    total_paused_seconds += running_seconds
    return True, running_seconds, total_paused_seconds // 60


def _seconds_to_hhmmss(seconds: int) -> str:
    total = max(0, seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _minutes_to_hhmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    total = abs(minutes)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def _enum_value(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    return getattr(value, "value", str(value))


def _render_punch_state(employee: Employee):
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    pause_active, running_pause_seconds, paused_today_minutes = _pause_summary(events)
    today_local = datetime.now(_app_timezone()).date()
    active_shift = _current_shift_for_employee_day(employee, today_local)
    leave_policy_balances = _leave_policy_balances(employee, active_shift, today_local)
    work_balance_summary = _work_balance_summary(employee, today_local)
    return render_template(
        "employee/_punch_state.html",
        employee=employee,
        events=events,
        last_event=events[-1] if events else None,
        punch_buttons=PUNCH_BUTTONS,
        current_state=current_state,
        recent_punches=_recent_punches(events),
        pause_active=pause_active,
        running_pause_time=_seconds_to_hhmmss(running_pause_seconds),
        paused_today=_minutes_to_hhmm(paused_today_minutes),
        leave_policy_balances=leave_policy_balances,
        work_balance_summary=work_balance_summary,
    )


@bp.get("/me/today")
@login_required
@tenant_required
def me_today():
    employee = _employee_for_current_user()
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    pause_active, running_pause_seconds, paused_today_minutes = _pause_summary(events)
    today_local = datetime.now(_app_timezone()).date()
    active_shift = _current_shift_for_employee_day(employee, today_local)
    leave_policy_balances = _leave_policy_balances(employee, active_shift, today_local)
    work_balance_summary = _work_balance_summary(employee, today_local)
    return render_template(
        "employee/today.html",
        employee=employee,
        events=events,
        last_event=events[-1] if events else None,
        punch_buttons=PUNCH_BUTTONS,
        current_state=current_state,
        recent_punches=_recent_punches(events),
        pause_active=pause_active,
        running_pause_time=_seconds_to_hhmmss(running_pause_seconds),
        paused_today=_minutes_to_hhmm(paused_today_minutes),
        leave_policy_balances=leave_policy_balances,
        work_balance_summary=work_balance_summary,
    )


@bp.post("/me/pause/toggle")
@login_required
@tenant_required
def toggle_pause():
    employee = _employee_for_current_user()
    events = _todays_events(employee.id)
    pause_active, _, _ = _pause_summary(events)
    event_type = TimeEventType.BREAK_END if pause_active else TimeEventType.BREAK_START

    event = TimeEvent(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        type=event_type,
        source=TimeEventSource.WEB,
        meta_json={"via": "employee_pause_control"},
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

    return redirect(url_for("employee.me_today"))


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

    event_local_ts = datetime.combine(event_date, time(hour=hour, minute=minute), tzinfo=_app_timezone())
    event_ts = event_local_ts.astimezone(timezone.utc)
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
    today_local = datetime.now(_app_timezone()).date()

    if requested_month:
        try:
            selected_year, selected_month = map(int, requested_month.split("-", 1))
        except ValueError:
            selected_year = today_local.year
            selected_month = today_local.month
    else:
        selected_year = today_local.year
        selected_month = today_local.month

    if (selected_year, selected_month) > (today_local.year, today_local.month):
        return redirect(url_for("employee.presence_control", month=f"{today_local.year:04d}-{today_local.month:02d}"))

    month_start, month_end = _month_bounds_utc(selected_year, selected_month)
    month_events_stmt = (
        select(TimeEvent)
        .where(TimeEvent.employee_id == employee.id, TimeEvent.ts >= month_start, TimeEvent.ts <= month_end)
        .order_by(TimeEvent.ts.asc())
    )
    month_events = list(db.session.execute(month_events_stmt).scalars().all())
    events_by_day: dict[date, list[TimeEvent]] = {}
    for event in month_events:
        events_by_day.setdefault(_to_app_tz(event.ts).date(), []).append(event)

    days_in_month = monthrange(selected_year, selected_month)[1]
    month_start_day = date(selected_year, selected_month, 1)
    month_end_day = date(selected_year, selected_month, days_in_month)
    assignment_rows = _employee_shift_assignments(employee.id, month_start_day, month_end_day)
    fallback_shift: Shift | None = None
    if assignment_rows is None:
        assignment_rows = []
        fallback_shift = _tenant_shift(employee.tenant_id)

    if fallback_shift is not None:
        active_shift = fallback_shift
    elif month_start_day <= today_local <= month_end_day:
        active_shift = _shift_for_day(assignment_rows, today_local)
    else:
        today_assignment_rows = _employee_shift_assignments(employee.id, today_local, today_local)
        if today_assignment_rows is None:
            active_shift = _tenant_shift(employee.tenant_id)
        else:
            active_shift = _shift_for_day(today_assignment_rows, today_local)

    active_shift_frequency = (
        SHIFT_FREQUENCY_LABELS.get(active_shift.expected_hours_frequency) if active_shift is not None else None
    )
    business_days_in_month = _business_days_in_month(selected_year, selected_month)
    business_days_in_year = _business_days_in_year(selected_year)
    month_rows = []
    month_worked = 0
    month_expected = 0
    for day_index in range(days_in_month):
        current_day = date(selected_year, selected_month, day_index + 1)
        day_events = events_by_day.get(current_day, [])
        is_open_day = (selected_year, selected_month) == (today_local.year, today_local.month) and current_day >= today_local
        if is_open_day:
            month_rows.append(
                {
                    "day": current_day,
                    "pairs": _daily_punch_markers(day_events),
                    "worked": None,
                    "expected": None,
                    "balance": None,
                    "has_manual": any(bool((event.meta_json or {}).get("manual")) for event in day_events),
                    "is_open_day": True,
                }
            )
            continue

        day_shift = fallback_shift if fallback_shift is not None else _shift_for_day(assignment_rows, current_day)
        worked_minutes, day_pairs, includes_manual = _daily_worked_minutes(day_events)
        paused_minutes, _ = _daily_pause_minutes(day_events)
        if day_shift is not None and not day_shift.break_counts_as_worked_bool:
            worked_minutes = max(0, worked_minutes - paused_minutes)
        expected_minutes = _expected_work_minutes_for_day(
            day_shift,
            current_day,
            business_days_in_month,
            business_days_in_year,
        )
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
                "is_open_day": False,
            }
        )

    recent_stmt = select(TimeEvent).where(TimeEvent.employee_id == employee.id).order_by(TimeEvent.ts.desc()).limit(12)
    recent_events = list(db.session.execute(recent_stmt).scalars().all())
    prev_month = (month_start - timedelta(days=1)).strftime("%Y-%m")
    next_month = (month_end + timedelta(days=1)).strftime("%Y-%m")
    can_go_next_month = (selected_year, selected_month) < (today_local.year, today_local.month)

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
        to_app_tz=_to_app_tz,
        prev_month=prev_month,
        next_month=next_month,
        minutes_to_hhmm=_minutes_to_hhmm,
        active_shift=active_shift,
        active_shift_frequency=active_shift_frequency,
        can_go_next_month=can_go_next_month,
    )


@bp.get("/me/pause-control")
@login_required
@tenant_required
def pause_control():
    employee = _employee_for_current_user()
    requested_month = request.args.get("month")
    today_local = datetime.now(_app_timezone()).date()

    if requested_month:
        try:
            selected_year, selected_month = map(int, requested_month.split("-", 1))
        except ValueError:
            selected_year = today_local.year
            selected_month = today_local.month
    else:
        selected_year = today_local.year
        selected_month = today_local.month

    if (selected_year, selected_month) > (today_local.year, today_local.month):
        return redirect(url_for("employee.pause_control", month=f"{today_local.year:04d}-{today_local.month:02d}"))

    month_start, month_end = _month_bounds_utc(selected_year, selected_month)
    month_events_stmt = (
        select(TimeEvent)
        .where(TimeEvent.employee_id == employee.id, TimeEvent.ts >= month_start, TimeEvent.ts <= month_end)
        .order_by(TimeEvent.ts.asc())
    )
    month_events = list(db.session.execute(month_events_stmt).scalars().all())
    events_by_day: dict[date, list[TimeEvent]] = {}
    for event in month_events:
        events_by_day.setdefault(_to_app_tz(event.ts).date(), []).append(event)

    days_in_month = monthrange(selected_year, selected_month)[1]
    month_start_day = date(selected_year, selected_month, 1)
    month_end_day = date(selected_year, selected_month, days_in_month)
    assignment_rows = _employee_shift_assignments(employee.id, month_start_day, month_end_day)
    fallback_shift: Shift | None = None
    if assignment_rows is None:
        assignment_rows = []
        fallback_shift = _tenant_shift(employee.tenant_id)

    if fallback_shift is not None:
        active_shift = fallback_shift
    elif month_start_day <= today_local <= month_end_day:
        active_shift = _shift_for_day(assignment_rows, today_local)
    else:
        today_assignment_rows = _employee_shift_assignments(employee.id, today_local, today_local)
        if today_assignment_rows is None:
            active_shift = _tenant_shift(employee.tenant_id)
        else:
            active_shift = _shift_for_day(today_assignment_rows, today_local)

    month_rows = []
    month_paused = 0
    month_expected = 0
    for day_index in range(days_in_month):
        current_day = date(selected_year, selected_month, day_index + 1)
        day_events = events_by_day.get(current_day, [])
        is_open_day = (selected_year, selected_month) == (today_local.year, today_local.month) and current_day >= today_local
        if is_open_day:
            _, pause_pairs = _daily_pause_minutes(day_events)
            month_rows.append(
                {
                    "day": current_day,
                    "pairs": pause_pairs,
                    "paused": None,
                    "expected": None,
                    "balance": None,
                    "is_open_day": True,
                }
            )
            continue

        day_shift = fallback_shift if fallback_shift is not None else _shift_for_day(assignment_rows, current_day)
        paused_minutes, day_pairs = _daily_pause_minutes(day_events)
        expected_minutes = _expected_pause_minutes_for_day(day_shift, current_day)
        month_paused += paused_minutes
        month_expected += expected_minutes
        month_rows.append(
            {
                "day": current_day,
                "pairs": day_pairs,
                "paused": paused_minutes,
                "expected": expected_minutes,
                "balance": paused_minutes - expected_minutes,
                "is_open_day": False,
            }
        )

    prev_month = (month_start - timedelta(days=1)).strftime("%Y-%m")
    next_month = (month_end + timedelta(days=1)).strftime("%Y-%m")
    can_go_next_month = (selected_year, selected_month) < (today_local.year, today_local.month)

    return render_template(
        "employee/pause_control.html",
        employee=employee,
        month_rows=month_rows,
        selected_month=f"{selected_year:04d}-{selected_month:02d}",
        month_label=month_start.strftime("%B %Y"),
        month_paused=_minutes_to_hhmm(month_paused),
        month_expected=_minutes_to_hhmm(month_expected),
        month_balance=_minutes_to_hhmm(month_paused - month_expected),
        prev_month=prev_month,
        next_month=next_month,
        minutes_to_hhmm=_minutes_to_hhmm,
        active_shift=active_shift,
        can_go_next_month=can_go_next_month,
    )


@bp.route("/me/leaves", methods=["GET", "POST"])
@login_required
@tenant_required
def me_leaves():
    employee = _employee_for_current_user()
    form = LeaveRequestForm()
    form.type_id.label.text = "Vacaciones / permisos"
    form.submit.label.text = "Enviar solicitud"

    today_local = datetime.now(_app_timezone()).date()
    active_shift = _current_shift_for_employee_day(employee, today_local)
    available_policies = _active_leave_policies_for_shift(active_shift, today_local)
    policy_by_id = {str(policy.id): policy for policy in available_policies}
    form.type_id.choices = [
        (
            str(policy.id),
            (
                f"{policy.name} - {_format_decimal_amount(Decimal(policy.amount))} "
                f"{LEAVE_POLICY_UNIT_LABELS.get(policy.unit, 'dias')} "
                f"({policy.valid_from.isoformat()} a {policy.valid_to.isoformat()})"
            ),
        )
        for policy in available_policies
    ]

    if request.method == "POST" and not available_policies:
        flash("No hay vacaciones o permisos definidos para tu turno actual.", "warning")
    elif form.validate_on_submit():
        selected_policy = policy_by_id.get(form.type_id.data)
        if selected_policy is None:
            flash("Vacaciones o permiso invalido para tu turno actual.", "danger")
        else:
            requested_from = form.date_from.data
            requested_to = form.date_to.data
            if requested_from < selected_policy.valid_from or requested_to > selected_policy.valid_to:
                flash("Las fechas solicitadas estan fuera del rango permitido para esta bolsa.", "danger")
            else:
                requested_amount, amount_error = _requested_amount_for_policy(
                    selected_policy,
                    requested_from,
                    requested_to,
                    form.minutes.data,
                )
                if amount_error is not None or requested_amount is None:
                    flash(amount_error or "Importe solicitado invalido.", "danger")
                else:
                    if _has_leave_overlap(employee.id, selected_policy.id, requested_from, requested_to):
                        flash(
                            "Ya existe una solicitud pendiente o aprobada que se solapa con estas fechas.",
                            "danger",
                        )
                        return redirect(url_for("employee.me_leaves"))

                    consumption = _policy_consumption_by_employee(employee.id, [selected_policy])
                    totals = consumption.get(selected_policy.id, {"approved": Decimal("0"), "pending": Decimal("0")})
                    used = totals["approved"] + totals["pending"]
                    total_amount = Decimal(selected_policy.amount)
                    if used + requested_amount > total_amount + Decimal("0.000001"):
                        flash("No hay saldo suficiente en esta bolsa para esa solicitud.", "danger")
                    else:
                        leave_request = LeaveRequest(
                            tenant_id=employee.tenant_id,
                            employee_id=employee.id,
                            type_id=selected_policy.leave_type_id,
                            leave_policy_id=selected_policy.id,
                            date_from=requested_from,
                            date_to=requested_to,
                            minutes=form.minutes.data if selected_policy.unit == LeavePolicyUnit.HOURS else None,
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
                                "type_id": str(selected_policy.leave_type_id),
                                "leave_policy_id": str(selected_policy.id),
                                "leave_policy_name": selected_policy.name,
                                "date_from": requested_from.isoformat(),
                                "date_to": requested_to.isoformat(),
                                "minutes": leave_request.minutes,
                                "status": leave_request.status.value,
                            },
                        )
                        db.session.commit()
                        flash("Solicitud registrada.", "success")
                        return redirect(url_for("employee.me_leaves"))

    history_stmt = (
        select(LeaveRequest, LeaveType)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(LeaveRequest.employee_id == employee.id)
        .order_by(LeaveRequest.created_at.desc())
    )
    requests_rows = db.session.execute(history_stmt).all()
    return render_template(
        "employee/leaves.html",
        form=form,
        rows=requests_rows,
        employee=employee,
        active_shift=active_shift,
        available_policies=available_policies,
        leave_status_labels=LEAVE_STATUS_LABELS,
    )


@bp.post("/me/leaves/<uuid:leave_request_id>/cancel")
@login_required
@tenant_required
def leave_cancel(leave_request_id: uuid.UUID):
    employee = _employee_for_current_user()
    leave_request = db.session.execute(
        select(LeaveRequest).where(
            LeaveRequest.id == leave_request_id,
            LeaveRequest.employee_id == employee.id,
        )
    ).scalar_one_or_none()
    if leave_request is None:
        abort(404)
    if leave_request.status != LeaveRequestStatus.REQUESTED:
        abort(409, description="La solicitud ya fue decidida.")

    leave_request.status = LeaveRequestStatus.CANCELLED
    leave_request.decided_at = datetime.now(timezone.utc)
    log_audit(
        action="LEAVE_CANCELLED",
        entity_type="leave_requests",
        entity_id=leave_request.id,
        payload={
            "employee_id": str(employee.id),
            "type_id": str(leave_request.type_id),
            "leave_policy_id": str(leave_request.leave_policy_id) if leave_request.leave_policy_id else None,
            "date_from": leave_request.date_from.isoformat(),
            "date_to": leave_request.date_to.isoformat(),
            "minutes": leave_request.minutes,
            "status": leave_request.status.value,
        },
    )
    db.session.commit()
    flash("Solicitud cancelada.", "success")
    return redirect(url_for("employee.me_leaves"))
