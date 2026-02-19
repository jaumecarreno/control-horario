"""Employee self-service routes."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import io
import mimetypes
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from werkzeug.utils import secure_filename

from app.audit import log_audit
from app.authorization import employee_self_service_required, manual_punch_required
from app.extensions import db
from app.forms import LeaveRequestForm, PunchCorrectionRequestForm
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    Membership,
    MembershipRole,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Shift,
    ShiftLeavePolicy,
    TimeEvent,
    TimeEventSource,
    TimeEventSupersession,
    TimeEventType,
    User,
)
from app.time_events import visible_employee_events_between_stmt, visible_employee_recent_events_stmt
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
PUNCH_CORRECTION_STATUS_LABELS = {
    PunchCorrectionStatus.REQUESTED: "Pendiente",
    PunchCorrectionStatus.APPROVED: "Aprobada",
    PunchCorrectionStatus.REJECTED: "Rechazada",
    PunchCorrectionStatus.CANCELLED: "Cancelada",
}
HOURS_PERIOD_OPTIONS = [
    {"value": "day", "label": "Dia"},
    {"value": "week", "label": "Semana"},
    {"value": "month", "label": "Mes"},
    {"value": "year", "label": "Ano"},
    {"value": "custom", "label": "Rango"},
]
HOURS_PERIOD_PRESETS = {"day", "week", "month", "year"}
REQUEST_ATTACHMENT_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
REQUEST_ATTACHMENT_ALLOWED_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
REQUEST_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024


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
    stmt = visible_employee_events_between_stmt(employee_id, start, end)
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


def _safe_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_range_bounds_utc(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    tz = _app_timezone()
    start_local = datetime.combine(start_day, time.min, tzinfo=tz)
    end_local = datetime.combine(end_day, time.max, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _iter_days(start_day: date, end_day: date) -> list[date]:
    total_days = (end_day - start_day).days
    return [start_day + timedelta(days=offset) for offset in range(total_days + 1)]


def _add_months(anchor: date, months_delta: int) -> date:
    months_since_year_zero = (anchor.year * 12) + (anchor.month - 1) + months_delta
    next_year = months_since_year_zero // 12
    next_month = (months_since_year_zero % 12) + 1
    last_day = monthrange(next_year, next_month)[1]
    next_day = min(anchor.day, last_day)
    return date(next_year, next_month, next_day)


def _hours_range_for_preset(preset: str, anchor: date) -> tuple[date, date]:
    if preset == "day":
        return anchor, anchor

    if preset == "week":
        start_day = anchor - timedelta(days=anchor.weekday())
        return start_day, start_day + timedelta(days=6)

    if preset == "year":
        start_day = date(anchor.year, 1, 1)
        return start_day, date(anchor.year, 12, 31)

    start_day = date(anchor.year, anchor.month, 1)
    return start_day, date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1])


def _daily_is_open(events: list[TimeEvent]) -> bool:
    open_presence = False
    open_pause = False

    for event in events:
        if event.type == TimeEventType.IN:
            open_presence = True
            continue

        if event.type == TimeEventType.OUT:
            open_presence = False
            open_pause = False
            continue

        if event.type == TimeEventType.BREAK_START:
            open_pause = True
            continue

        if event.type == TimeEventType.BREAK_END:
            open_pause = False

    return open_presence or open_pause


def _open_shift_started_at(events: list[TimeEvent]) -> datetime | None:
    for event in reversed(events):
        if event.type == TimeEventType.OUT:
            return None
        if event.type == TimeEventType.IN:
            return _to_app_tz(event.ts)
    return None


def _extract_optional_attachment():
    file_storage = request.files.get("attachment")
    if file_storage is None or not (file_storage.filename or "").strip():
        return None, None, None, None

    raw_filename = secure_filename((file_storage.filename or "").strip())
    if not raw_filename:
        return None, None, None, "Nombre de adjunto invalido."

    extension = Path(raw_filename).suffix.lower()
    if extension not in REQUEST_ATTACHMENT_ALLOWED_EXTENSIONS:
        return None, None, None, "El adjunto debe ser PDF o imagen JPG/PNG/WEBP."

    mime_type = (file_storage.mimetype or "").strip().lower()
    guessed_mime, _ = mimetypes.guess_type(raw_filename)
    guessed_mime = (guessed_mime or "").lower()
    if not mime_type:
        mime_type = guessed_mime
    if mime_type not in REQUEST_ATTACHMENT_ALLOWED_MIME and guessed_mime in REQUEST_ATTACHMENT_ALLOWED_MIME:
        mime_type = guessed_mime
    if mime_type not in REQUEST_ATTACHMENT_ALLOWED_MIME:
        return None, None, None, "Tipo de adjunto no permitido. Solo PDF o imagen."

    payload = file_storage.read()
    if not payload:
        return None, None, None, "El adjunto no puede estar vacio."
    if len(payload) > REQUEST_ATTACHMENT_MAX_BYTES:
        return None, None, None, "El adjunto supera el maximo permitido de 5MB."

    return raw_filename, mime_type, payload, None


def _hours_selection_from_request(args, today_local: date) -> dict[str, object]:
    selected_preset = (args.get("preset") or "month").strip().lower()
    anchor = _safe_iso_date(args.get("anchor")) or today_local
    if anchor > today_local:
        anchor = today_local

    raw_date_from = args.get("date_from", "")
    raw_date_to = args.get("date_to", "")
    custom_start = _safe_iso_date(raw_date_from)
    custom_end = _safe_iso_date(raw_date_to)

    if (selected_preset == "custom" or (custom_start and custom_end)) and custom_start and custom_end:
        start_day, end_day = sorted((custom_start, custom_end))
        return {
            "preset": "custom",
            "anchor": anchor,
            "start_day": start_day,
            "end_day": end_day,
            "date_from_value": start_day.isoformat(),
            "date_to_value": end_day.isoformat(),
        }

    if selected_preset not in HOURS_PERIOD_PRESETS:
        selected_preset = "month"

    start_day, end_day = _hours_range_for_preset(selected_preset, anchor)
    return {
        "preset": selected_preset,
        "anchor": anchor,
        "start_day": start_day,
        "end_day": end_day,
        "date_from_value": "",
        "date_to_value": "",
    }


def _hours_nav_queries(selection: dict[str, object], today_local: date) -> dict[str, object]:
    preset = str(selection["preset"])
    anchor = selection["anchor"]
    start_day = selection["start_day"]
    end_day = selection["end_day"]
    assert isinstance(anchor, date)
    assert isinstance(start_day, date)
    assert isinstance(end_day, date)

    if preset == "custom":
        span_days = (end_day - start_day).days + 1
        prev_start = start_day - timedelta(days=span_days)
        prev_end = end_day - timedelta(days=span_days)
        next_start = start_day + timedelta(days=span_days)
        next_end = end_day + timedelta(days=span_days)
        return {
            "prev_query": {
                "preset": "custom",
                "anchor": anchor.isoformat(),
                "date_from": prev_start.isoformat(),
                "date_to": prev_end.isoformat(),
            },
            "next_query": {
                "preset": "custom",
                "anchor": anchor.isoformat(),
                "date_from": next_start.isoformat(),
                "date_to": next_end.isoformat(),
            },
            "can_go_next": next_start <= today_local,
        }

    if preset == "day":
        prev_anchor = anchor - timedelta(days=1)
        next_anchor = anchor + timedelta(days=1)
    elif preset == "week":
        prev_anchor = anchor - timedelta(days=7)
        next_anchor = anchor + timedelta(days=7)
    elif preset == "year":
        prev_year = anchor.year - 1
        next_year = anchor.year + 1
        prev_day = min(anchor.day, monthrange(prev_year, anchor.month)[1])
        next_day = min(anchor.day, monthrange(next_year, anchor.month)[1])
        prev_anchor = date(prev_year, anchor.month, prev_day)
        next_anchor = date(next_year, anchor.month, next_day)
    else:
        prev_anchor = _add_months(anchor, -1)
        next_anchor = _add_months(anchor, 1)

    next_start_day, _ = _hours_range_for_preset(preset, next_anchor)
    return {
        "prev_query": {"preset": preset, "anchor": prev_anchor.isoformat()},
        "next_query": {"preset": preset, "anchor": next_anchor.isoformat()},
        "can_go_next": next_start_day <= today_local,
    }


def _hours_calendar_payload(
    rows: list[dict[str, object]],
    start_day: date,
    end_day: date,
    today_local: date,
) -> dict[str, object] | None:
    if start_day.year != end_day.year or start_day.month != end_day.month:
        return None

    first_day = date(start_day.year, start_day.month, 1)
    days_in_month = monthrange(start_day.year, start_day.month)[1]
    weekday_offset = first_day.weekday()
    rows_by_day = {str(row["date"]): row for row in rows}

    cells: list[dict[str, object] | None] = [None] * weekday_offset
    for day_number in range(1, days_in_month + 1):
        cell_day = date(start_day.year, start_day.month, day_number)
        day_key = cell_day.isoformat()
        row = rows_by_day.get(day_key)
        has_events = bool(row and (row["in_out"] or row["pauses"]))
        cells.append(
            {
                "day_number": day_number,
                "date": day_key,
                "is_today": cell_day == today_local,
                "is_open": bool(row and row["is_open"]),
                "has_events": has_events,
                "net_display": str(row["net_display"]) if row and has_events else "--:--",
            }
        )

    while len(cells) % 7 != 0:
        cells.append(None)

    return {
        "month_label": first_day.strftime("%B %Y"),
        "weekdays": ["L", "M", "X", "J", "V", "S", "D"],
        "weeks": [cells[index : index + 7] for index in range(0, len(cells), 7)],
    }


def _build_hours_payload(employee: Employee, args) -> dict[str, object]:
    today_local = datetime.now(_app_timezone()).date()
    selection = _hours_selection_from_request(args, today_local)
    start_day = selection["start_day"]
    end_day = selection["end_day"]
    assert isinstance(start_day, date)
    assert isinstance(end_day, date)

    start_utc, end_utc = _date_range_bounds_utc(start_day, end_day)
    events_stmt = visible_employee_events_between_stmt(employee.id, start_utc, end_utc)
    period_events = list(db.session.execute(events_stmt).scalars().all())

    events_by_day: dict[date, list[TimeEvent]] = {}
    for event in period_events:
        events_by_day.setdefault(_to_app_tz(event.ts).date(), []).append(event)

    rows: list[dict[str, object]] = []
    total_worked = 0
    total_paused = 0
    total_net = 0

    for current_day in _iter_days(start_day, end_day):
        day_events = events_by_day.get(current_day, [])
        worked_minutes, _, _ = _daily_worked_minutes(day_events)
        paused_minutes, pause_pairs = _daily_pause_minutes(day_events)
        net_minutes = worked_minutes - paused_minutes
        total_worked += worked_minutes
        total_paused += paused_minutes
        total_net += net_minutes

        rows.append(
            {
                "date": current_day.isoformat(),
                "label": current_day.strftime("%d/%m/%Y"),
                "in_out": _daily_punch_markers(day_events),
                "pauses": pause_pairs,
                "worked_minutes": worked_minutes,
                "paused_minutes": paused_minutes,
                "net_minutes": net_minutes,
                "worked_display": _minutes_to_hhmm(worked_minutes),
                "paused_display": _minutes_to_hhmm(paused_minutes),
                "net_display": _minutes_to_hhmm(net_minutes),
                "is_open": _daily_is_open(day_events),
            }
        )

    nav_data = _hours_nav_queries(selection, today_local)
    calendar = _hours_calendar_payload(rows, start_day, end_day, today_local)
    preset = str(selection["preset"])
    today_events = _todays_events(employee.id)
    open_shift_started = _open_shift_started_at(today_events)
    open_shift_started_display = open_shift_started.strftime("%d/%m/%Y %H:%M") if open_shift_started else None

    return {
        "preset": preset,
        "period_options": HOURS_PERIOD_OPTIONS,
        "anchor": selection["anchor"].isoformat(),
        "date_from_value": selection["date_from_value"],
        "date_to_value": selection["date_to_value"],
        "period_start": start_day.isoformat(),
        "period_end": end_day.isoformat(),
        "period_label": f"{start_day.strftime('%d/%m/%Y')} - {end_day.strftime('%d/%m/%Y')}",
        "days": rows,
        "totals": {
            "worked_minutes": total_worked,
            "paused_minutes": total_paused,
            "net_minutes": total_net,
            "worked_display": _minutes_to_hhmm(total_worked),
            "paused_display": _minutes_to_hhmm(total_paused),
            "net_display": _minutes_to_hhmm(total_net),
        },
        "calendar": calendar,
        "can_go_next": bool(nav_data["can_go_next"]),
        "prev_query": nav_data["prev_query"],
        "next_query": nav_data["next_query"],
        "prev_url": url_for("employee.me_hours", **nav_data["prev_query"]),
        "next_url": url_for("employee.me_hours", **nav_data["next_query"]),
        "open_shift_started": open_shift_started_display,
        "today": today_local.isoformat(),
    }


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
    month_events_stmt = visible_employee_events_between_stmt(employee.id, month_start_utc, day_end_utc)
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

    if requested_from != requested_to:
        return None, "Para permisos en horas debes usar el mismo dia en desde/hasta."

    if requested_minutes is None or requested_minutes <= 0:
        return None, "Para permisos en horas debes indicar minutos mayores que cero."
    return Decimal(requested_minutes) / Decimal(60), None


def _has_leave_overlap(
    employee_id: uuid.UUID,
    leave_policy_id: uuid.UUID,
    requested_from: date,
    requested_to: date,
    *,
    exclude_request_id: uuid.UUID | None = None,
) -> bool:
    where_conditions = [
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.leave_policy_id == leave_policy_id,
        LeaveRequest.status.in_([LeaveRequestStatus.REQUESTED, LeaveRequestStatus.APPROVED]),
        LeaveRequest.date_from <= requested_to,
        LeaveRequest.date_to >= requested_from,
    ]
    if exclude_request_id is not None:
        where_conditions.append(LeaveRequest.id != exclude_request_id)

    stmt = select(LeaveRequest.id).where(*where_conditions).limit(1)
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
    open_shift_started = _open_shift_started_at(events)
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
        open_shift_started=open_shift_started,
        leave_policy_balances=leave_policy_balances,
        work_balance_summary=work_balance_summary,
    )


@bp.get("/me/today")
@login_required
@tenant_required
@employee_self_service_required
def me_today():
    employee = _employee_for_current_user()
    events = _todays_events(employee.id)
    current_state = _current_presence_state(events)
    open_shift_started = _open_shift_started_at(events)
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
        open_shift_started=open_shift_started,
        leave_policy_balances=leave_policy_balances,
        work_balance_summary=work_balance_summary,
    )


@bp.get("/me/hours")
@login_required
@tenant_required
@employee_self_service_required
def me_hours():
    employee = _employee_for_current_user()
    payload = _build_hours_payload(employee, request.args)
    return render_template("employee/hours.html", employee=employee, **payload)


@bp.get("/me/hours/data")
@login_required
@tenant_required
@employee_self_service_required
def me_hours_data():
    employee = _employee_for_current_user()
    return _build_hours_payload(employee, request.args)


@bp.post("/me/pause/toggle")
@login_required
@tenant_required
@employee_self_service_required
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
@employee_self_service_required
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
@employee_self_service_required
def me_events():
    employee = _employee_for_current_user()
    stmt = visible_employee_recent_events_stmt(employee.id, 100)
    events = list(db.session.execute(stmt).scalars().all())
    return render_template("employee/events.html", employee=employee, events=events)


@bp.post("/me/incidents/manual")
@login_required
@tenant_required
@manual_punch_required
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


def _presence_month_redirect(selected_month: str | None) -> str:
    if selected_month:
        return url_for("employee.presence_control", month=selected_month)
    return url_for("employee.presence_control")


def _resolve_target_punch_approver(employee: Employee) -> uuid.UUID | None:
    if employee.punch_approver_user_id is None:
        return None

    approver_membership = db.session.execute(
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == employee.tenant_id,
            Membership.user_id == employee.punch_approver_user_id,
            Membership.role.in_((MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER)),
            User.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if approver_membership is None:
        return None
    return employee.punch_approver_user_id


@bp.post("/me/punch-corrections")
@login_required
@tenant_required
@employee_self_service_required
def punch_correction_request_create():
    employee = _employee_for_current_user()
    form = PunchCorrectionRequestForm()
    selected_month = request.form.get("return_month")

    if not form.validate_on_submit():
        flash("Solicitud de rectificacion invalida.", "danger")
        return redirect(_presence_month_redirect(selected_month))

    source_event_id = uuid.UUID(form.source_event_id.data.strip())
    source_event = db.session.execute(
        select(TimeEvent).where(
            TimeEvent.id == source_event_id,
            TimeEvent.tenant_id == employee.tenant_id,
            TimeEvent.employee_id == employee.id,
        )
    ).scalar_one_or_none()
    if source_event is None:
        abort(404)
    if source_event.type not in {TimeEventType.IN, TimeEventType.OUT}:
        flash("Solo puedes rectificar fichajes de entrada o salida.", "danger")
        return redirect(_presence_month_redirect(selected_month))

    source_local_day = _to_app_tz(source_event.ts).date()
    today_local = datetime.now(_app_timezone()).date()
    if source_local_day > today_local or (today_local - source_local_day).days > 30:
        flash("Solo se permiten rectificaciones de fichajes dentro de los ultimos 30 dias.", "danger")
        return redirect(_presence_month_redirect(selected_month))

    existing_supersession = db.session.execute(
        select(TimeEventSupersession).where(TimeEventSupersession.original_event_id == source_event.id)
    ).scalar_one_or_none()
    if existing_supersession is not None:
        flash("Ese fichaje ya fue rectificado.", "warning")
        return redirect(_presence_month_redirect(selected_month))

    existing_pending = db.session.execute(
        select(PunchCorrectionRequest.id).where(
            PunchCorrectionRequest.source_event_id == source_event.id,
            PunchCorrectionRequest.status == PunchCorrectionStatus.REQUESTED,
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        flash("Ya existe una solicitud pendiente para ese fichaje.", "warning")
        return redirect(_presence_month_redirect(selected_month))

    requested_type = TimeEventType(form.requested_kind.data)
    requested_local_ts = datetime.combine(
        form.requested_date.data,
        time(hour=form.requested_hour.data, minute=form.requested_minute.data),
        tzinfo=_app_timezone(),
    )
    requested_ts = requested_local_ts.astimezone(timezone.utc)

    target_approver_user_id = _resolve_target_punch_approver(employee)
    actor_user_id = uuid.UUID(current_user.get_id())
    if target_approver_user_id is not None and target_approver_user_id == actor_user_id:
        flash(
            "No puedes ser tu propio aprobador de rectificaciones. Pide a un admin que cambie la configuracion.",
            "danger",
        )
        return redirect(_presence_month_redirect(selected_month))

    attachment_name, attachment_mime, attachment_blob, attachment_error = _extract_optional_attachment()
    if attachment_error is not None:
        flash(attachment_error, "danger")
        return redirect(_presence_month_redirect(selected_month))

    correction_request = PunchCorrectionRequest(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        source_event_id=source_event.id,
        requested_ts=requested_ts,
        requested_type=requested_type,
        reason=form.reason.data.strip(),
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
        attachment_blob=attachment_blob,
        status=PunchCorrectionStatus.REQUESTED,
        target_approver_user_id=target_approver_user_id,
    )
    db.session.add(correction_request)
    db.session.flush()
    log_audit(
        action="PUNCH_CORRECTION_REQUESTED",
        entity_type="punch_correction_requests",
        entity_id=correction_request.id,
        payload={
            "employee_id": str(employee.id),
            "source_event_id": str(source_event.id),
            "source_event_type": source_event.type.value,
            "source_event_ts": source_event.ts.isoformat(),
            "requested_ts": requested_ts.isoformat(),
            "requested_type": requested_type.value,
            "reason": correction_request.reason,
            "has_attachment": bool(correction_request.attachment_blob),
            "attachment_name": correction_request.attachment_name,
            "target_approver_user_id": str(target_approver_user_id) if target_approver_user_id else None,
            "status": correction_request.status.value,
        },
    )
    db.session.commit()
    flash("Solicitud de rectificacion enviada.", "success")
    return redirect(_presence_month_redirect(selected_month))


@bp.post("/me/punch-corrections/<uuid:correction_request_id>/cancel")
@login_required
@tenant_required
@employee_self_service_required
def punch_correction_request_cancel(correction_request_id: uuid.UUID):
    employee = _employee_for_current_user()
    selected_month = request.form.get("return_month")

    correction_request = db.session.execute(
        select(PunchCorrectionRequest).where(
            PunchCorrectionRequest.id == correction_request_id,
            PunchCorrectionRequest.employee_id == employee.id,
            PunchCorrectionRequest.tenant_id == employee.tenant_id,
        )
    ).scalar_one_or_none()
    if correction_request is None:
        abort(404)
    if correction_request.status != PunchCorrectionStatus.REQUESTED:
        abort(409, description="La solicitud ya fue decidida.")

    correction_request.status = PunchCorrectionStatus.CANCELLED
    correction_request.decided_at = datetime.now(timezone.utc)
    log_audit(
        action="PUNCH_CORRECTION_CANCELLED",
        entity_type="punch_correction_requests",
        entity_id=correction_request.id,
        payload={
            "employee_id": str(employee.id),
            "source_event_id": str(correction_request.source_event_id),
            "requested_ts": correction_request.requested_ts.isoformat(),
            "requested_type": correction_request.requested_type.value,
            "status": correction_request.status.value,
        },
    )
    db.session.commit()
    flash("Solicitud de rectificacion cancelada.", "success")
    return redirect(_presence_month_redirect(selected_month))


@bp.get("/me/punch-corrections/<uuid:correction_request_id>/attachment")
@login_required
@tenant_required
@employee_self_service_required
def punch_correction_attachment_download(correction_request_id: uuid.UUID):
    employee = _employee_for_current_user()
    correction_request = db.session.execute(
        select(PunchCorrectionRequest).where(
            PunchCorrectionRequest.id == correction_request_id,
            PunchCorrectionRequest.employee_id == employee.id,
            PunchCorrectionRequest.tenant_id == employee.tenant_id,
        )
    ).scalar_one_or_none()
    if correction_request is None or correction_request.attachment_blob is None:
        abort(404)

    download_name = correction_request.attachment_name or "adjunto-rectificacion"
    mime_type = correction_request.attachment_mime or "application/octet-stream"
    return send_file(
        io.BytesIO(correction_request.attachment_blob),
        mimetype=mime_type,
        as_attachment=True,
        download_name=download_name,
    )


@bp.get("/me/presence-control")
@login_required
@tenant_required
@employee_self_service_required
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
    month_events_stmt = visible_employee_events_between_stmt(employee.id, month_start, month_end)
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

    recent_stmt = visible_employee_recent_events_stmt(employee.id, 12)
    recent_events = list(db.session.execute(recent_stmt).scalars().all())
    correction_rows_stmt = (
        select(PunchCorrectionRequest, TimeEvent)
        .join(TimeEvent, TimeEvent.id == PunchCorrectionRequest.source_event_id)
        .where(PunchCorrectionRequest.employee_id == employee.id)
        .order_by(PunchCorrectionRequest.created_at.desc())
        .limit(20)
    )
    try:
        correction_rows = db.session.execute(correction_rows_stmt).all()
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Punch correction history lookup failed. "
            "Run `alembic upgrade head` to apply pending migrations.",
            exc_info=True,
        )
        correction_rows = []
    punch_correction_form = PunchCorrectionRequestForm()
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
        correction_rows=correction_rows,
        punch_correction_form=punch_correction_form,
        punch_correction_status_labels=PUNCH_CORRECTION_STATUS_LABELS,
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
@employee_self_service_required
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
    month_events_stmt = visible_employee_events_between_stmt(employee.id, month_start, month_end)
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


def _leave_form_setup(employee: Employee, form: LeaveRequestForm, today_local: date):
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
    return active_shift, available_policies, policy_by_id


@bp.route("/me/leaves", methods=["GET", "POST"])
@login_required
@tenant_required
@employee_self_service_required
def me_leaves():
    employee = _employee_for_current_user()
    form = LeaveRequestForm()
    form.type_id.label.text = "Vacaciones / permisos"
    form.submit.label.text = "Enviar solicitud"

    today_local = datetime.now(_app_timezone()).date()
    active_shift, available_policies, policy_by_id = _leave_form_setup(employee, form, today_local)

    if request.method == "POST" and not available_policies:
        flash("No hay vacaciones o permisos definidos para tu turno actual.", "warning")
    elif form.validate_on_submit():
        selected_policy = policy_by_id.get(form.type_id.data)
        if selected_policy is None:
            flash("Vacaciones o permiso invalido para tu turno actual.", "danger")
        else:
            attachment_name, attachment_mime, attachment_blob, attachment_error = _extract_optional_attachment()
            if attachment_error is not None:
                flash(attachment_error, "danger")
                return redirect(url_for("employee.me_leaves"))

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
                            reason=form.reason.data.strip(),
                            attachment_name=attachment_name,
                            attachment_mime=attachment_mime,
                            attachment_blob=attachment_blob,
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
                                "reason": leave_request.reason,
                                "has_attachment": bool(leave_request.attachment_blob),
                                "attachment_name": leave_request.attachment_name,
                                "minutes": leave_request.minutes,
                                "status": leave_request.status.value,
                            },
                        )
                        db.session.commit()
                        flash("Solicitud registrada.", "success")
                        return redirect(url_for("employee.me_leaves"))
    elif request.method == "POST":
        flash("Solicitud invalida. Revisa motivo, fechas y minutos.", "danger")

    history_stmt = (
        select(LeaveRequest, LeaveType)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(LeaveRequest.employee_id == employee.id)
        .order_by(LeaveRequest.created_at.desc())
    )
    try:
        requests_rows = db.session.execute(history_stmt).all()
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Leave history lookup failed. "
            "Run `alembic upgrade head` to apply pending migrations.",
            exc_info=True,
        )
        requests_rows = []
    return render_template(
        "employee/leaves.html",
        form=form,
        rows=requests_rows,
        employee=employee,
        active_shift=active_shift,
        available_policies=available_policies,
        leave_status_labels=LEAVE_STATUS_LABELS,
    )


@bp.route("/me/leaves/<uuid:leave_request_id>/edit", methods=["GET", "POST"])
@login_required
@tenant_required
@employee_self_service_required
def leave_edit(leave_request_id: uuid.UUID):
    employee = _employee_for_current_user()
    leave_request = db.session.execute(
        select(LeaveRequest).where(
            LeaveRequest.id == leave_request_id,
            LeaveRequest.employee_id == employee.id,
            LeaveRequest.tenant_id == employee.tenant_id,
        )
    ).scalar_one_or_none()
    if leave_request is None:
        abort(404)
    if leave_request.status != LeaveRequestStatus.REQUESTED:
        abort(409, description="Solo puedes editar solicitudes pendientes.")

    form = LeaveRequestForm()
    form.type_id.label.text = "Vacaciones / permisos"
    form.submit.label.text = "Guardar cambios"

    today_local = datetime.now(_app_timezone()).date()
    active_shift, available_policies, policy_by_id = _leave_form_setup(employee, form, today_local)

    if leave_request.leave_policy_id is not None and str(leave_request.leave_policy_id) not in policy_by_id:
        existing_policy = db.session.get(ShiftLeavePolicy, leave_request.leave_policy_id)
        if existing_policy is not None:
            policy_by_id[str(existing_policy.id)] = existing_policy
            form.type_id.choices.append(
                (
                    str(existing_policy.id),
                    (
                        f"{existing_policy.name} - {_format_decimal_amount(Decimal(existing_policy.amount))} "
                        f"{LEAVE_POLICY_UNIT_LABELS.get(existing_policy.unit, 'dias')} "
                        f"({existing_policy.valid_from.isoformat()} a {existing_policy.valid_to.isoformat()}) "
                        "(no activa)"
                    ),
                )
            )

    if request.method == "GET":
        if leave_request.leave_policy_id is not None:
            form.type_id.data = str(leave_request.leave_policy_id)
        form.date_from.data = leave_request.date_from
        form.date_to.data = leave_request.date_to
        form.minutes.data = leave_request.minutes
        form.reason.data = leave_request.reason
    elif form.validate_on_submit():
        selected_policy = policy_by_id.get(form.type_id.data)
        if selected_policy is None:
            flash("Vacaciones o permiso invalido para tu turno actual.", "danger")
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        attachment_name, attachment_mime, attachment_blob, attachment_error = _extract_optional_attachment()
        if attachment_error is not None:
            flash(attachment_error, "danger")
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        requested_from = form.date_from.data
        requested_to = form.date_to.data
        if requested_from < selected_policy.valid_from or requested_to > selected_policy.valid_to:
            flash("Las fechas solicitadas estan fuera del rango permitido para esta bolsa.", "danger")
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        requested_amount, amount_error = _requested_amount_for_policy(
            selected_policy,
            requested_from,
            requested_to,
            form.minutes.data,
        )
        if amount_error is not None or requested_amount is None:
            flash(amount_error or "Importe solicitado invalido.", "danger")
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        if _has_leave_overlap(
            employee.id,
            selected_policy.id,
            requested_from,
            requested_to,
            exclude_request_id=leave_request.id,
        ):
            flash(
                "Ya existe una solicitud pendiente o aprobada que se solapa con estas fechas.",
                "danger",
            )
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        consumption = _policy_consumption_by_employee(employee.id, [selected_policy])
        totals = consumption.get(selected_policy.id, {"approved": Decimal("0"), "pending": Decimal("0")})
        used = totals["approved"] + totals["pending"]
        if leave_request.leave_policy_id == selected_policy.id:
            current_amount, _ = _requested_amount_for_policy(
                selected_policy,
                leave_request.date_from,
                leave_request.date_to,
                leave_request.minutes,
            )
            if current_amount is not None:
                used -= current_amount
        total_amount = Decimal(selected_policy.amount)
        if used + requested_amount > total_amount + Decimal("0.000001"):
            flash("No hay saldo suficiente en esta bolsa para esa solicitud.", "danger")
            return redirect(url_for("employee.leave_edit", leave_request_id=leave_request.id))

        before_payload = {
            "type_id": str(leave_request.type_id),
            "leave_policy_id": str(leave_request.leave_policy_id) if leave_request.leave_policy_id else None,
            "date_from": leave_request.date_from.isoformat(),
            "date_to": leave_request.date_to.isoformat(),
            "reason": leave_request.reason,
            "minutes": leave_request.minutes,
            "attachment_name": leave_request.attachment_name,
        }

        leave_request.type_id = selected_policy.leave_type_id
        leave_request.leave_policy_id = selected_policy.id
        leave_request.date_from = requested_from
        leave_request.date_to = requested_to
        leave_request.reason = form.reason.data.strip()
        leave_request.minutes = form.minutes.data if selected_policy.unit == LeavePolicyUnit.HOURS else None
        if attachment_blob is not None:
            leave_request.attachment_name = attachment_name
            leave_request.attachment_mime = attachment_mime
            leave_request.attachment_blob = attachment_blob
        elif request.form.get("remove_attachment") == "1":
            leave_request.attachment_name = None
            leave_request.attachment_mime = None
            leave_request.attachment_blob = None

        after_payload = {
            "type_id": str(leave_request.type_id),
            "leave_policy_id": str(leave_request.leave_policy_id) if leave_request.leave_policy_id else None,
            "date_from": leave_request.date_from.isoformat(),
            "date_to": leave_request.date_to.isoformat(),
            "reason": leave_request.reason,
            "minutes": leave_request.minutes,
            "attachment_name": leave_request.attachment_name,
        }
        log_audit(
            action="LEAVE_UPDATED",
            entity_type="leave_requests",
            entity_id=leave_request.id,
            payload={
                "employee_id": str(employee.id),
                "before": before_payload,
                "after": after_payload,
            },
        )
        db.session.commit()
        flash("Solicitud actualizada.", "success")
        return redirect(url_for("employee.me_leaves"))
    elif request.method == "POST":
        flash("Solicitud invalida. Revisa motivo, fechas y minutos.", "danger")

    return render_template(
        "employee/leave_edit.html",
        form=form,
        employee=employee,
        leave_request=leave_request,
        active_shift=active_shift,
        available_policies=available_policies,
    )


@bp.get("/me/leaves/<uuid:leave_request_id>/attachment")
@login_required
@tenant_required
@employee_self_service_required
def leave_attachment_download(leave_request_id: uuid.UUID):
    employee = _employee_for_current_user()
    leave_request = db.session.execute(
        select(LeaveRequest).where(
            LeaveRequest.id == leave_request_id,
            LeaveRequest.employee_id == employee.id,
            LeaveRequest.tenant_id == employee.tenant_id,
        )
    ).scalar_one_or_none()
    if leave_request is None or leave_request.attachment_blob is None:
        abort(404)

    download_name = leave_request.attachment_name or "adjunto-ausencia"
    mime_type = leave_request.attachment_mime or "application/octet-stream"
    return send_file(
        io.BytesIO(leave_request.attachment_blob),
        mimetype=mime_type,
        as_attachment=True,
        download_name=download_name,
    )


@bp.post("/me/leaves/<uuid:leave_request_id>/cancel")
@login_required
@tenant_required
@employee_self_service_required
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
            "reason": leave_request.reason,
            "minutes": leave_request.minutes,
            "status": leave_request.status.value,
        },
    )
    db.session.commit()
    flash("Solicitud cancelada.", "success")
    return redirect(url_for("employee.me_leaves"))
