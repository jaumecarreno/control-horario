"""Admin routes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.audit import log_audit
from app.authorization import (
    approve_leaves_required,
    approve_punch_corrections_required,
    export_payroll_required,
    manage_employees_required,
    manage_shifts_required,
    manage_users_required,
    view_adjustments_required,
)
from app.extensions import db
from app.forms import (
    AdminResetPasswordForm,
    AttendanceReportForm,
    EmployeeCreateForm,
    EmployeeEditForm,
    ShiftCreateForm,
    UserCreateForm,
    UserEditForm,
)
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Membership,
    MembershipRole,
    Shift,
    User,
    ShiftLeavePolicy,
    TimeAdjustment,
    TimeEvent,
    TimeEventSource,
    TimeEventSupersession,
    TimeEventType,
)
from app.report_export import to_csv_bytes, to_json_bytes, to_pdf_bytes, to_xlsx_bytes
from app.security import hash_secret
from app.tenant import get_active_tenant_id, tenant_required
from app.time_events import visible_events_with_employee_between_stmt


bp = Blueprint("admin", __name__)

SHIFT_FREQUENCY_LABELS = {
    ExpectedHoursFrequency.YEARLY: "Anuales",
    ExpectedHoursFrequency.MONTHLY: "Mensuales",
    ExpectedHoursFrequency.WEEKLY: "Semanales",
    ExpectedHoursFrequency.DAILY: "Diarias",
}
LEAVE_POLICY_UNIT_LABELS = {
    LeavePolicyUnit.DAYS: "Dias",
    LeavePolicyUnit.HOURS: "Horas",
}
LEAVE_POLICY_UNIT_CHOICES = [
    (LeavePolicyUnit.DAYS.value, LEAVE_POLICY_UNIT_LABELS[LeavePolicyUnit.DAYS]),
    (LeavePolicyUnit.HOURS.value, LEAVE_POLICY_UNIT_LABELS[LeavePolicyUnit.HOURS]),
]
PUNCH_CORRECTION_STATUS_LABELS = {
    PunchCorrectionStatus.REQUESTED: "Pendiente",
    PunchCorrectionStatus.APPROVED: "Aprobada",
    PunchCorrectionStatus.REJECTED: "Rechazada",
    PunchCorrectionStatus.CANCELLED: "Cancelada",
}
REPORT_TYPE_LABELS = {
    "control": "Control horario",
    "executive": "Resumen ejecutivo",
}
REPORT_FORMAT_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
REPORT_FORMAT_EXTENSIONS = {
    "csv": "csv",
    "json": "json",
    "xlsx": "xlsx",
    "pdf": "pdf",
}


def _policy_default_dates(reference_day: date | None = None) -> tuple[str, str]:
    current = reference_day or date.today()
    return date(current.year, 1, 1).isoformat(), date(current.year, 12, 21).isoformat()


def _enum_value(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    return getattr(value, "value", str(value))


def _tenant_shifts(tenant_id: UUID) -> list[Shift]:
    return list(db.session.execute(select(Shift).where(Shift.tenant_id == tenant_id).order_by(Shift.name.asc())).scalars().all())


def _shift_leave_policies(shift_id: UUID) -> list[ShiftLeavePolicy]:
    stmt = (
        select(ShiftLeavePolicy)
        .where(ShiftLeavePolicy.shift_id == shift_id)
        .order_by(ShiftLeavePolicy.created_at.asc(), ShiftLeavePolicy.name.asc())
    )
    return list(db.session.execute(stmt).scalars().all())


def _new_blank_policy_row() -> dict[str, str]:
    default_valid_from, default_valid_to = _policy_default_dates()
    return {
        "name": "",
        "amount": "",
        "unit": LeavePolicyUnit.DAYS.value,
        "valid_from": default_valid_from,
        "valid_to": default_valid_to,
    }


def _policy_rows_from_models(rows: list[ShiftLeavePolicy]) -> list[dict[str, str]]:
    formatted_rows: list[dict[str, str]] = []
    for row in rows:
        formatted_rows.append(
            {
                "name": row.name,
                "amount": format(row.amount, "f"),
                "unit": _enum_value(row.unit, fallback=LeavePolicyUnit.DAYS.value),
                "valid_from": row.valid_from.isoformat(),
                "valid_to": row.valid_to.isoformat(),
            }
        )
    return formatted_rows


def _parse_shift_leave_policy_rows() -> tuple[list[dict[str, object]], list[dict[str, str]], list[str]]:
    names = request.form.getlist("policy_name")
    amounts = request.form.getlist("policy_amount")
    units = request.form.getlist("policy_unit")
    valid_from_values = request.form.getlist("policy_valid_from")
    valid_to_values = request.form.getlist("policy_valid_to")

    max_rows = max(len(names), len(amounts), len(units), len(valid_from_values), len(valid_to_values), 0)
    parsed_rows: list[dict[str, object]] = []
    raw_rows: list[dict[str, str]] = []
    errors: list[str] = []

    for index in range(max_rows):
        raw_name = (names[index] if index < len(names) else "").strip()
        raw_amount = (amounts[index] if index < len(amounts) else "").strip()
        raw_unit = (units[index] if index < len(units) else LeavePolicyUnit.DAYS.value).strip() or LeavePolicyUnit.DAYS.value
        raw_valid_from = (valid_from_values[index] if index < len(valid_from_values) else "").strip()
        raw_valid_to = (valid_to_values[index] if index < len(valid_to_values) else "").strip()
        raw_row = {
            "name": raw_name,
            "amount": raw_amount,
            "unit": raw_unit,
            "valid_from": raw_valid_from,
            "valid_to": raw_valid_to,
        }
        raw_rows.append(raw_row)

        if not raw_name and not raw_amount and not raw_valid_from and not raw_valid_to:
            continue

        row_number = index + 1
        if not raw_name:
            errors.append(f"Fila {row_number}: el nombre es obligatorio.")
            continue
        if not raw_amount:
            errors.append(f"Fila {row_number}: el numero de dias u horas es obligatorio.")
            continue
        if not raw_valid_from or not raw_valid_to:
            errors.append(f"Fila {row_number}: inicio y fin son obligatorios.")
            continue

        try:
            amount = Decimal(raw_amount.replace(",", "."))
        except InvalidOperation:
            errors.append(f"Fila {row_number}: numero de dias u horas invalido.")
            continue
        if amount <= 0:
            errors.append(f"Fila {row_number}: el numero de dias u horas debe ser mayor que cero.")
            continue

        try:
            unit = LeavePolicyUnit(raw_unit)
        except ValueError:
            errors.append(f"Fila {row_number}: unidad invalida.")
            continue

        try:
            valid_from = date.fromisoformat(raw_valid_from)
            valid_to = date.fromisoformat(raw_valid_to)
        except ValueError:
            errors.append(f"Fila {row_number}: fecha invalida.")
            continue
        if valid_to < valid_from:
            errors.append(f"Fila {row_number}: la fecha fin no puede ser anterior al inicio.")
            continue

        parsed_rows.append(
            {
                "name": raw_name,
                "amount": amount,
                "unit": unit,
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )

    return parsed_rows, raw_rows, errors


def _leave_type_code_base(name: str) -> str:
    code = "".join(char for char in name.upper() if char.isalnum())
    if not code:
        return "PERMISO"
    return code[:32]


def _get_or_create_leave_type(tenant_id: UUID, policy_name: str) -> LeaveType:
    normalized_name = policy_name.strip()
    existing = db.session.execute(
        select(LeaveType).where(LeaveType.tenant_id == tenant_id, func.lower(LeaveType.name) == normalized_name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    code_base = _leave_type_code_base(normalized_name)
    candidate = code_base
    sequence = 1
    while (
        db.session.execute(select(LeaveType.id).where(LeaveType.tenant_id == tenant_id, LeaveType.code == candidate)).scalar_one_or_none()
        is not None
    ):
        sequence += 1
        suffix = f"-{sequence}"
        candidate = f"{code_base[:32 - len(suffix)]}{suffix}"

    leave_type = LeaveType(
        tenant_id=tenant_id,
        code=candidate,
        name=normalized_name,
        paid_bool=False,
        requires_approval_bool=True,
        counts_as_worked_bool=False,
    )
    db.session.add(leave_type)
    db.session.flush()
    return leave_type


def _replace_shift_leave_policies(tenant_id: UUID, shift_id: UUID, rows: list[dict[str, object]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    existing_rows = _shift_leave_policies(shift_id)
    before_payload = [
        {
            "name": item.name,
            "amount": str(item.amount),
            "unit": _enum_value(item.unit),
            "valid_from": item.valid_from.isoformat(),
            "valid_to": item.valid_to.isoformat(),
            "leave_type_id": str(item.leave_type_id),
        }
        for item in existing_rows
    ]
    for item in existing_rows:
        db.session.delete(item)

    after_payload: list[dict[str, str]] = []
    for row in rows:
        leave_type = _get_or_create_leave_type(tenant_id, str(row["name"]))
        policy = ShiftLeavePolicy(
            tenant_id=tenant_id,
            shift_id=shift_id,
            leave_type_id=leave_type.id,
            name=str(row["name"]),
            amount=Decimal(row["amount"]),
            unit=LeavePolicyUnit(row["unit"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )
        db.session.add(policy)
        after_payload.append(
            {
                "name": policy.name,
                "amount": str(policy.amount),
                "unit": _enum_value(policy.unit),
                "valid_from": policy.valid_from.isoformat(),
                "valid_to": policy.valid_to.isoformat(),
                "leave_type_id": str(leave_type.id),
                "leave_type_code": leave_type.code,
            }
        )

    return before_payload, after_payload


def _employee_assignment_rows(employee_id: UUID) -> list[tuple[EmployeeShiftAssignment, Shift | None]]:
    stmt = (
        select(EmployeeShiftAssignment, Shift)
        .outerjoin(Shift, Shift.id == EmployeeShiftAssignment.shift_id)
        .where(EmployeeShiftAssignment.employee_id == employee_id)
        .order_by(EmployeeShiftAssignment.effective_from.desc(), EmployeeShiftAssignment.created_at.desc())
    )
    return list(db.session.execute(stmt).all())


def _set_employee_shift_assignment(employee: Employee, shift: Shift, effective_from: date) -> bool:
    assignments = list(
        db.session.execute(
            select(EmployeeShiftAssignment)
            .where(EmployeeShiftAssignment.employee_id == employee.id)
            .order_by(EmployeeShiftAssignment.effective_from.asc(), EmployeeShiftAssignment.created_at.asc())
        )
        .scalars()
        .all()
    )

    changed = False
    for assignment in assignments:
        if assignment.effective_from >= effective_from:
            db.session.delete(assignment)
            changed = True

    previous = next((item for item in reversed(assignments) if item.effective_from < effective_from), None)
    if previous is not None:
        if previous.shift_id == shift.id:
            if previous.effective_to is not None:
                previous.effective_to = None
                changed = True
            return changed
        new_previous_end = effective_from - timedelta(days=1)
        if previous.effective_to != new_previous_end:
            previous.effective_to = new_previous_end
            changed = True

    db.session.add(
        EmployeeShiftAssignment(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            shift_id=shift.id,
            effective_from=effective_from,
            effective_to=None,
        )
    )
    return True


def _current_shift_names_by_employee(employee_ids: list[UUID], today: date) -> dict[UUID, str]:
    if not employee_ids:
        return {}

    stmt = (
        select(EmployeeShiftAssignment, Shift)
        .join(Shift, Shift.id == EmployeeShiftAssignment.shift_id)
        .where(
            EmployeeShiftAssignment.employee_id.in_(employee_ids),
            EmployeeShiftAssignment.effective_from <= today,
            or_(EmployeeShiftAssignment.effective_to.is_(None), EmployeeShiftAssignment.effective_to >= today),
        )
        .order_by(
            EmployeeShiftAssignment.employee_id.asc(),
            EmployeeShiftAssignment.effective_from.desc(),
            EmployeeShiftAssignment.created_at.desc(),
        )
    )
    rows = db.session.execute(stmt).all()
    current_shift_by_employee: dict[UUID, str] = {}
    for assignment, shift in rows:
        current_shift_by_employee.setdefault(assignment.employee_id, shift.name)
    return current_shift_by_employee



def _employee_choices(tenant_id: UUID) -> list[tuple[str, str]]:
    rows = list(db.session.execute(select(Employee).where(Employee.tenant_id == tenant_id).order_by(Employee.name.asc())).scalars().all())
    return [(str(employee.id), f"{employee.name} ({employee.email or '-'})") for employee in rows]


def _punch_approver_choices(tenant_id: UUID) -> list[tuple[str, str]]:
    rows = list(
        db.session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.role.in_((MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER)),
                User.is_active.is_(True),
            )
            .order_by(User.email.asc())
        ).all()
    )
    return [("", "Fallback a admins del tenant")] + [
        (str(user.id), f"{user.email} ({membership.role.value})") for membership, user in rows
    ]


def _tenant_admin_count(tenant_id: UUID) -> int:
    return int(
        db.session.execute(
            select(func.count(Membership.id)).where(
                Membership.tenant_id == tenant_id,
                Membership.role.in_((MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER)),
            )
        ).scalar_one()
    )


def _can_manage_owner_transition(actor_role: MembershipRole, current_role: MembershipRole, new_role: MembershipRole) -> bool:
    owner_change = current_role is MembershipRole.OWNER or new_role is MembershipRole.OWNER
    if not owner_change:
        return True
    return actor_role is MembershipRole.OWNER


def _would_remove_last_admin_access(tenant_id: UUID, target_user_id: UUID, new_role: MembershipRole) -> bool:
    if new_role in {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}:
        return False

    membership = db.session.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == target_user_id)
    ).scalar_one_or_none()
    if membership is None:
        return False
    if membership.role not in {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}:
        return False

    return _tenant_admin_count(tenant_id) <= 1


def _report_timezone() -> ZoneInfo:
    tz_name = current_app.config.get("APP_TIMEZONE", "Europe/Madrid")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_utc(ts: datetime) -> datetime:
    aware_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return aware_ts.astimezone(timezone.utc)


def _to_report_tz(ts: datetime) -> datetime:
    return _as_utc(ts).astimezone(_report_timezone())


def _report_window_utc(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    report_tz = _report_timezone()
    start_local = datetime.combine(date_from, time.min, tzinfo=report_tz)
    end_local = datetime.combine(date_to, time.max, tzinfo=report_tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _attendance_report_employees(tenant_id: UUID, selected_employee_id: UUID | None) -> list[Employee]:
    stmt = select(Employee).where(Employee.tenant_id == tenant_id).order_by(Employee.name.asc(), Employee.id.asc())
    if selected_employee_id is not None:
        stmt = stmt.where(Employee.id == selected_employee_id)
    return list(db.session.execute(stmt).scalars().all())


def _attendance_report_events(
    tenant_id: UUID,
    start_utc: datetime,
    end_utc: datetime,
    selected_employee_id: UUID | None,
) -> list[tuple[TimeEvent, Employee]]:
    stmt = visible_events_with_employee_between_stmt(start_utc, end_utc).where(TimeEvent.tenant_id == tenant_id)
    if selected_employee_id is not None:
        stmt = stmt.where(TimeEvent.employee_id == selected_employee_id)
    return list(db.session.execute(stmt).all())


def _build_control_report_rows(event_rows: list[tuple[TimeEvent, Employee]]) -> tuple[list[str], list[list[str]]]:
    headers = [
        "employee_id",
        "employee_name",
        "event_id",
        "timestamp_utc",
        "timestamp_local",
        "event_type",
        "source",
        "manual",
    ]
    rows: list[list[str]] = []
    for event, employee in event_rows:
        rows.append(
            [
                str(employee.id),
                employee.name,
                str(event.id),
                _as_utc(event.ts).isoformat(),
                _to_report_tz(event.ts).strftime("%Y-%m-%d %H:%M:%S"),
                event.type.value,
                event.source.value,
                "yes" if bool((event.meta_json or {}).get("manual")) else "no",
            ]
        )
    return headers, rows


def _worked_minutes_from_events(events: list[TimeEvent]) -> int:
    worked_minutes = 0
    open_entry: TimeEvent | None = None

    for event in sorted(events, key=lambda row: _as_utc(row.ts)):
        if event.type == TimeEventType.IN:
            open_entry = event
            continue
        if event.type != TimeEventType.OUT or open_entry is None:
            continue

        delta = _as_utc(event.ts) - _as_utc(open_entry.ts)
        worked_minutes += max(0, int(delta.total_seconds() // 60))
        open_entry = None

    return worked_minutes


def _build_executive_report_rows(
    employees: list[Employee],
    event_rows: list[tuple[TimeEvent, Employee]],
) -> tuple[list[str], list[list[str | int]]]:
    headers = [
        "employee_id",
        "employee_name",
        "days_with_events",
        "total_events",
        "in_events",
        "out_events",
        "manual_events",
        "worked_minutes",
        "worked_hours",
        "first_event_local",
        "last_event_local",
    ]
    stats_by_employee: dict[UUID, dict[str, object]] = {}
    events_by_employee_day: dict[UUID, dict[date, list[TimeEvent]]] = {}

    for employee in employees:
        stats_by_employee[employee.id] = {
            "employee_name": employee.name,
            "total_events": 0,
            "in_events": 0,
            "out_events": 0,
            "manual_events": 0,
            "first_event_local": None,
            "last_event_local": None,
        }
        events_by_employee_day[employee.id] = {}

    for event, employee in event_rows:
        stats = stats_by_employee.setdefault(
            employee.id,
            {
                "employee_name": employee.name,
                "total_events": 0,
                "in_events": 0,
                "out_events": 0,
                "manual_events": 0,
                "first_event_local": None,
                "last_event_local": None,
            },
        )
        local_ts = _to_report_tz(event.ts)
        stats["total_events"] = int(stats["total_events"]) + 1
        if event.type == TimeEventType.IN:
            stats["in_events"] = int(stats["in_events"]) + 1
        elif event.type == TimeEventType.OUT:
            stats["out_events"] = int(stats["out_events"]) + 1
        if bool((event.meta_json or {}).get("manual")):
            stats["manual_events"] = int(stats["manual_events"]) + 1

        first_event_local = stats["first_event_local"]
        last_event_local = stats["last_event_local"]
        if first_event_local is None or local_ts < first_event_local:
            stats["first_event_local"] = local_ts
        if last_event_local is None or local_ts > last_event_local:
            stats["last_event_local"] = local_ts

        daily_events = events_by_employee_day.setdefault(employee.id, {})
        daily_events.setdefault(local_ts.date(), []).append(event)

    rows: list[list[str | int]] = []
    for employee in employees:
        stats = stats_by_employee.get(employee.id, {})
        employee_days = events_by_employee_day.get(employee.id, {})
        worked_minutes = sum(_worked_minutes_from_events(day_events) for day_events in employee_days.values())
        worked_hours = f"{worked_minutes / 60:.2f}"
        first_event_local = stats.get("first_event_local")
        last_event_local = stats.get("last_event_local")
        rows.append(
            [
                str(employee.id),
                str(stats.get("employee_name") or employee.name),
                len(employee_days),
                int(stats.get("total_events") or 0),
                int(stats.get("in_events") or 0),
                int(stats.get("out_events") or 0),
                int(stats.get("manual_events") or 0),
                worked_minutes,
                worked_hours,
                first_event_local.strftime("%Y-%m-%d %H:%M:%S") if first_event_local is not None else "",
                last_event_local.strftime("%Y-%m-%d %H:%M:%S") if last_event_local is not None else "",
            ]
        )
    return headers, rows


def _report_download_filename(
    report_type: str,
    output_format: str,
    date_from: date,
    date_to: date,
    selected_employee_id: UUID | None,
) -> str:
    employee_suffix = f"_{selected_employee_id}" if selected_employee_id is not None else "_all"
    ext = REPORT_FORMAT_EXTENSIONS[output_format]
    return f"{report_type}_report_{date_from.isoformat()}_{date_to.isoformat()}{employee_suffix}.{ext}"


@bp.get("/admin/users")
@login_required
@tenant_required
@manage_users_required
def users_list():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    rows = list(
        db.session.execute(
            select(Membership, User, Employee)
            .join(User, User.id == Membership.user_id)
            .outerjoin(Employee, Employee.id == Membership.employee_id)
            .where(Membership.tenant_id == tenant_id)
            .order_by(User.email.asc())
        ).all()
    )
    return render_template("admin/users.html", rows=rows)


@bp.route("/admin/users/new", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_users_required
def users_new():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    form = UserCreateForm()
    form.employee_id.choices = [("", "Sin empleado")] + _employee_choices(tenant_id)

    if form.validate_on_submit():
        normalized_email = form.email.data.strip().lower()
        password = form.password.data
        confirm_password = form.confirm_password.data

        if password != confirm_password:
            flash("La confirmacion de password no coincide.", "danger")
            return render_template("admin/user_new.html", form=form)

        existing = db.session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
        if existing is not None:
            flash("Ya existe un usuario con ese email.", "warning")
            return render_template("admin/user_new.html", form=form)

        try:
            selected_role = MembershipRole(form.role.data)
        except ValueError:
            flash("Rol invalido.", "danger")
            return render_template("admin/user_new.html", form=form)

        selected_employee_id = form.employee_id.data.strip() if form.employee_id.data else ""
        employee_id = None
        if selected_role is MembershipRole.EMPLOYEE:
            if not selected_employee_id:
                flash("Debe seleccionar un empleado para el rol EMPLOYEE.", "danger")
                return render_template("admin/user_new.html", form=form)
            try:
                parsed_employee_id = UUID(selected_employee_id)
            except ValueError:
                flash("Empleado invalido para el tenant actual.", "danger")
                return render_template("admin/user_new.html", form=form)
            employee = db.session.execute(
                select(Employee).where(Employee.id == parsed_employee_id, Employee.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if employee is None:
                flash("Empleado invalido para el tenant actual.", "danger")
                return render_template("admin/user_new.html", form=form)
            employee_id = employee.id
        elif selected_employee_id:
            flash("Los roles admin/manager/owner no deben tener empleado asociado.", "danger")
            return render_template("admin/user_new.html", form=form)

        user = User(
            email=normalized_email,
            password_hash=hash_secret(password),
            is_active=bool(form.active.data),
        )
        db.session.add(user)
        db.session.flush()

        membership = Membership(
            tenant_id=tenant_id,
            user_id=user.id,
            role=selected_role,
            employee_id=employee_id,
        )
        db.session.add(membership)
        db.session.commit()
        flash("Usuario creado.", "success")
        return redirect(url_for("admin.users_list"))

    return render_template("admin/user_new.html", form=form)


@bp.route("/admin/users/<uuid:user_id>/edit", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_users_required
def users_edit(user_id: UUID):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    row = db.session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
    ).one_or_none()
    if row is None:
        abort(404)
    membership, user = row

    actor_membership = db.session.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == UUID(current_user.get_id()))
    ).scalar_one_or_none()
    if actor_membership is None:
        abort(403, description="Insufficient permissions.")

    form = UserEditForm()
    form.employee_id.choices = [("", "Sin empleado")] + _employee_choices(tenant_id)

    if request.method == "GET":
        form.role.data = membership.role.value
        form.employee_id.data = str(membership.employee_id) if membership.employee_id else ""
        form.active.data = user.is_active

    if form.validate_on_submit():
        try:
            new_role = MembershipRole(form.role.data)
        except ValueError:
            flash("Rol invalido.", "danger")
            return render_template("admin/user_edit.html", form=form, user=user)

        if not _can_manage_owner_transition(actor_membership.role, membership.role, new_role):
            flash("Solo OWNER puede cambiar asignaciones de OWNER.", "danger")
            return render_template("admin/user_edit.html", form=form, user=user)

        selected_employee_id = (form.employee_id.data or "").strip()
        employee_id = None
        if new_role is MembershipRole.EMPLOYEE:
            try:
                parsed_employee_id = UUID(selected_employee_id)
            except ValueError:
                flash("Empleado invalido para el tenant actual.", "danger")
                return render_template("admin/user_edit.html", form=form, user=user)
            employee = db.session.execute(
                select(Employee).where(Employee.id == parsed_employee_id, Employee.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if employee is None:
                flash("Empleado invalido para el tenant actual.", "danger")
                return render_template("admin/user_edit.html", form=form, user=user)
            employee_id = employee.id

        if (
            membership.user_id == UUID(current_user.get_id())
            and _would_remove_last_admin_access(tenant_id, membership.user_id, new_role)
        ):
            flash("No puedes quitar tu ultimo acceso administrativo del tenant.", "danger")
            return render_template("admin/user_edit.html", form=form, user=user)

        previous_role = membership.role
        previous_employee_id = membership.employee_id
        previous_status = user.is_active

        membership.role = new_role
        membership.employee_id = employee_id
        user.is_active = bool(form.active.data)

        db.session.flush()

        if previous_role != membership.role or previous_employee_id != membership.employee_id:
            log_audit(
                action="USER_ROLE_CHANGED",
                entity_type="memberships",
                entity_id=membership.id,
                payload={
                    "user_id": str(user.id),
                    "before": {
                        "role": previous_role.value,
                        "employee_id": str(previous_employee_id) if previous_employee_id else None,
                    },
                    "after": {
                        "role": membership.role.value,
                        "employee_id": str(membership.employee_id) if membership.employee_id else None,
                    },
                },
            )

        if previous_status != user.is_active:
            log_audit(
                action="USER_STATUS_CHANGED",
                entity_type="users",
                entity_id=user.id,
                payload={
                    "before": {"is_active": previous_status},
                    "after": {"is_active": user.is_active},
                },
            )

        db.session.commit()
        flash("Usuario actualizado.", "success")
        return redirect(url_for("admin.users_list"))

    return render_template("admin/user_edit.html", form=form, user=user)



@bp.route("/admin/users/<uuid:user_id>/reset-password", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_users_required
def users_reset_password(user_id: UUID):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    row = db.session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
    ).one_or_none()
    if row is None:
        abort(404)
    membership, user = row

    form = AdminResetPasswordForm()
    if form.validate_on_submit():
        user.password_hash = hash_secret(form.temporary_password.data)
        user.must_change_password = True
        db.session.flush()

        log_audit(
            action="USER_PASSWORD_RESET",
            entity_type="users",
            entity_id=user.id,
            payload={
                "user_id": str(user.id),
                "membership_role": membership.role.value,
                "must_change_password": True,
            },
        )
        db.session.commit()
        flash("Contraseña temporal establecida. El usuario deberá cambiarla al iniciar sesión.", "success")
        return redirect(url_for("admin.users_list"))

    return render_template("admin/user_reset_password.html", form=form, user=user)


@bp.get("/admin/employees")
@login_required
@tenant_required
@manage_employees_required
def employees_list():
    employees = list(db.session.execute(select(Employee).order_by(Employee.name.asc())).scalars().all())
    active_employees = [employee for employee in employees if employee.active]
    inactive_employees = [employee for employee in employees if not employee.active]
    current_shift_by_employee: dict[UUID, str] = {}
    try:
        current_shift_by_employee = _current_shift_names_by_employee(
            [employee.id for employee in employees],
            date.today(),
        )
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Employee shift assignment lookup failed while listing employees.",
            exc_info=True,
        )
        flash("No se pudieron cargar los turnos asignados de empleados.", "warning")
    return render_template(
        "admin/employees.html",
        active_employees=active_employees,
        inactive_employees=inactive_employees,
        current_shift_by_employee=current_shift_by_employee,
    )


@bp.route("/admin/employees/new", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_employees_required
def employees_new():
    form = EmployeeCreateForm()
    if form.validate_on_submit():
        tenant_id = get_active_tenant_id()
        if tenant_id is None:
            abort(400, description="No active tenant selected.")

        employee = Employee(
            tenant_id=tenant_id,
            name=form.name.data.strip(),
            email=form.email.data.strip().lower() if form.email.data else None,
            pin_hash=hash_secret(form.pin.data) if form.pin.data else None,
            active=form.active.data,
        )
        db.session.add(employee)
        db.session.flush()
        log_audit(
            action="EMPLOYEE_CREATED",
            entity_type="employees",
            entity_id=employee.id,
            payload={"name": employee.name, "email": employee.email},
        )
        db.session.commit()
        flash("Employee created.", "success")
        return redirect(url_for("admin.employees_list"))
    return render_template("admin/employee_new.html", form=form)


@bp.route("/admin/employees/<uuid:employee_id>/edit", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_employees_required
def employees_edit(employee_id: UUID):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    employee = db.session.execute(
        select(Employee).where(Employee.id == employee_id, Employee.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if employee is None:
        abort(404)

    form = EmployeeEditForm()
    form.punch_approver_user_id.choices = _punch_approver_choices(tenant_id)

    tenant_shifts: list[Shift] = []
    shifts_available = True
    try:
        tenant_shifts = _tenant_shifts(tenant_id)
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        shifts_available = False
        current_app.logger.warning(
            "Shift lookup failed while editing employee.",
            exc_info=True,
        )
        flash("No se pudieron cargar los turnos disponibles.", "warning")

    form.assignment_shift_id.choices = [("", "No cambiar turno")] + [
        (str(shift.id), f"{shift.name} ({shift.expected_hours} {_enum_value(shift.expected_hours_frequency)})")
        for shift in tenant_shifts
    ]

    assignment_rows: list[tuple[EmployeeShiftAssignment, Shift | None]] = []
    assignment_history_available = True
    try:
        assignment_rows = _employee_assignment_rows(employee.id)
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        assignment_history_available = False
        current_app.logger.warning(
            "Shift assignment history lookup failed while editing employee.",
            exc_info=True,
        )
        flash("No se pudo cargar el historial de turnos del empleado.", "warning")

    if request.method == "GET":
        form.name.data = employee.name
        form.email.data = employee.email
        form.active.data = employee.active
        form.punch_approver_user_id.data = str(employee.punch_approver_user_id) if employee.punch_approver_user_id else ""
        form.assignment_effective_from.data = date.today()

    if form.validate_on_submit():
        employee_name = form.name.data.strip()
        if not employee_name:
            flash("El nombre del empleado es obligatorio.", "danger")
            return render_template(
                "admin/employee_edit.html",
                form=form,
                employee=employee,
                assignment_rows=assignment_rows,
            )

        employee_email = form.email.data.strip().lower() if form.email.data else None
        if employee_email:
            existing_email = db.session.execute(
                select(Employee).where(
                    Employee.tenant_id == tenant_id,
                    Employee.email == employee_email,
                    Employee.id != employee.id,
                )
            ).scalar_one_or_none()
            if existing_email is not None:
                flash("Ya existe otro empleado con ese email.", "warning")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )

        employee.name = employee_name
        employee.email = employee_email
        new_active_status = bool(form.active.data)
        if employee.active != new_active_status:
            employee.active_status_changed_at = datetime.now(timezone.utc)
        employee.active = new_active_status

        selected_approver_user_id_raw = (form.punch_approver_user_id.data or "").strip()
        selected_approver_user_id: UUID | None = None
        if selected_approver_user_id_raw:
            try:
                selected_approver_user_id = UUID(selected_approver_user_id_raw)
            except ValueError:
                flash("Aprobador de rectificaciones invalido.", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )

            approver_membership = db.session.execute(
                select(Membership)
                .join(User, User.id == Membership.user_id)
                .where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == selected_approver_user_id,
                    Membership.role.in_((MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER)),
                    User.is_active.is_(True),
                )
            ).scalar_one_or_none()
            if approver_membership is None:
                flash("Aprobador de rectificaciones invalido para este tenant.", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )
        employee.punch_approver_user_id = selected_approver_user_id

        if form.pin.data:
            employee.pin_hash = hash_secret(form.pin.data)

        shift_payload: dict[str, str] | None = None
        if form.assignment_shift_id.data:
            if not shifts_available:
                flash("No se pudo actualizar turno porque no se pueden consultar los turnos.", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )
            if not assignment_history_available:
                flash("No se pudo actualizar turno. Revisa migraciones pendientes (alembic upgrade head).", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )
            effective_from = form.assignment_effective_from.data
            if effective_from is None:
                flash("La fecha de inicio del nuevo turno es obligatoria.", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )

            selected_shift = next((item for item in tenant_shifts if str(item.id) == form.assignment_shift_id.data), None)
            if selected_shift is None:
                flash("Turno seleccionado inválido.", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )
            try:
                if _set_employee_shift_assignment(employee, selected_shift, effective_from):
                    shift_payload = {
                        "shift_id": str(selected_shift.id),
                        "shift_name": selected_shift.name,
                        "effective_from": effective_from.isoformat(),
                    }
            except (OperationalError, ProgrammingError, LookupError):
                db.session.rollback()
                current_app.logger.warning(
                    "Shift assignment update failed while editing employee.",
                    exc_info=True,
                )
                flash("No se pudo actualizar turno. Revisa migraciones pendientes (alembic upgrade head).", "danger")
                return render_template(
                    "admin/employee_edit.html",
                    form=form,
                    employee=employee,
                    assignment_rows=assignment_rows,
                )

        db.session.flush()
        log_audit(
            action="EMPLOYEE_UPDATED",
            entity_type="employees",
            entity_id=employee.id,
            payload={
                "name": employee.name,
                "email": employee.email,
                "active": employee.active,
                "active_status_changed_at": employee.active_status_changed_at.isoformat(),
                "punch_approver_user_id": str(employee.punch_approver_user_id) if employee.punch_approver_user_id else None,
                "pin_updated": bool(form.pin.data),
            },
        )
        if shift_payload is not None:
            log_audit(
                action="EMPLOYEE_SHIFT_ASSIGNED",
                entity_type="employee_shift_assignments",
                entity_id=employee.id,
                payload=shift_payload,
            )
        db.session.commit()
        flash("Empleado actualizado.", "success")
        return redirect(url_for("admin.employees_edit", employee_id=employee.id))

    return render_template(
        "admin/employee_edit.html",
        form=form,
        employee=employee,
        assignment_rows=assignment_rows,
    )


@bp.get("/admin/team-today")
@login_required
@tenant_required
@manage_employees_required
def team_today():
    return redirect(url_for("admin.shifts"))


@bp.get("/admin/turnos")
@login_required
@tenant_required
@manage_shifts_required
def shifts():
    return _render_turnos()


def _render_turnos():
    try:
        rows = list(db.session.execute(select(Shift).order_by(Shift.name.asc())).scalars().all())
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Shift list failed. Falling back to empty list. "
            "Run `alembic upgrade head` to apply pending migrations.",
            exc_info=True,
        )
        flash("No se pudieron cargar los turnos. Revisa migraciones pendientes (alembic upgrade head).", "warning")
        rows = []
    return render_template("admin/shifts.html", rows=rows, shift_frequency_labels=SHIFT_FREQUENCY_LABELS)


@bp.route("/admin/turnos/new", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_shifts_required
def shifts_new():
    form = ShiftCreateForm()
    policy_default_valid_from, policy_default_valid_to = _policy_default_dates()
    parsed_policy_rows: list[dict[str, object]] = []
    raw_policy_rows: list[dict[str, str]] = []
    policy_errors: list[str] = []
    if request.method == "POST":
        parsed_policy_rows, raw_policy_rows, policy_errors = _parse_shift_leave_policy_rows()

    policy_rows_for_template = raw_policy_rows if raw_policy_rows else [_new_blank_policy_row()]
    if form.validate_on_submit():
        tenant_id = get_active_tenant_id()
        if tenant_id is None:
            abort(400, description="No active tenant selected.")

        shift_name = form.name.data.strip()
        if not shift_name:
            flash("El nombre del turno es obligatorio.", "danger")
            return render_template(
                "admin/shift_new.html",
                form=form,
                policy_rows=policy_rows_for_template,
                policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                policy_default_valid_from=policy_default_valid_from,
                policy_default_valid_to=policy_default_valid_to,
            )
        if policy_errors:
            for message in policy_errors:
                flash(message, "danger")
            return render_template(
                "admin/shift_new.html",
                form=form,
                policy_rows=policy_rows_for_template,
                policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                policy_default_valid_from=policy_default_valid_from,
                policy_default_valid_to=policy_default_valid_to,
            )
        try:
            existing_shift = db.session.execute(
                select(Shift).where(Shift.tenant_id == tenant_id, Shift.name == shift_name)
            ).scalar_one_or_none()
            if existing_shift is not None:
                flash("Ya existe un turno con ese nombre.", "warning")
                return render_template(
                    "admin/shift_new.html",
                    form=form,
                    policy_rows=policy_rows_for_template,
                    policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                    policy_default_valid_from=policy_default_valid_from,
                    policy_default_valid_to=policy_default_valid_to,
                )

            expected_frequency = ExpectedHoursFrequency(form.expected_hours_frequency.data)
            shift = Shift(
                tenant_id=tenant_id,
                name=shift_name,
                break_counts_as_worked_bool=bool(form.break_counts_as_worked_bool.data),
                break_minutes=int(form.break_minutes.data),
                expected_hours=form.expected_hours.data,
                expected_hours_frequency=expected_frequency,
            )
            db.session.add(shift)
            db.session.flush()
            _, saved_policies_payload = _replace_shift_leave_policies(tenant_id, shift.id, parsed_policy_rows)
            log_audit(
                action="SHIFT_CREATED",
                entity_type="shifts",
                entity_id=shift.id,
                payload={
                    "name": shift.name,
                    "break_counts_as_worked_bool": shift.break_counts_as_worked_bool,
                    "break_minutes": shift.break_minutes,
                    "expected_hours": str(shift.expected_hours),
                    "expected_hours_frequency": shift.expected_hours_frequency.value,
                    "leave_policies": saved_policies_payload,
                },
            )
            db.session.commit()
            flash("Turno creado.", "success")
            return redirect(url_for("admin.shifts"))
        except (OperationalError, ProgrammingError, LookupError):
            db.session.rollback()
            current_app.logger.warning(
                "Shift create failed. Database schema likely out of date.",
                exc_info=True,
            )
            flash("No se pudo crear el turno. Revisa migraciones pendientes (alembic upgrade head).", "danger")
            return render_template(
                "admin/shift_new.html",
                form=form,
                policy_rows=policy_rows_for_template,
                policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                policy_default_valid_from=policy_default_valid_from,
                policy_default_valid_to=policy_default_valid_to,
            )

    return render_template(
        "admin/shift_new.html",
        form=form,
        policy_rows=policy_rows_for_template,
        policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
        policy_default_valid_from=policy_default_valid_from,
        policy_default_valid_to=policy_default_valid_to,
    )


@bp.route("/admin/turnos/<uuid:shift_id>/edit", methods=["GET", "POST"])
@login_required
@tenant_required
@manage_shifts_required
def shifts_edit(shift_id: UUID):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")
    policy_default_valid_from, policy_default_valid_to = _policy_default_dates()

    try:
        shift = db.session.execute(
            select(Shift).where(Shift.id == shift_id, Shift.tenant_id == tenant_id)
        ).scalar_one_or_none()
    except (OperationalError, ProgrammingError, LookupError):
        db.session.rollback()
        current_app.logger.warning(
            "Shift lookup failed while editing shift.",
            exc_info=True,
        )
        flash("No se pudo cargar el turno. Revisa migraciones pendientes (alembic upgrade head).", "danger")
        return redirect(url_for("admin.shifts"))

    if shift is None:
        abort(404)

    form = ShiftCreateForm()
    form.submit.label.text = "Guardar cambios"
    parsed_policy_rows: list[dict[str, object]] = []
    raw_policy_rows: list[dict[str, str]] = []
    policy_errors: list[str] = []
    existing_policy_rows: list[dict[str, str]] = []
    if request.method == "POST":
        parsed_policy_rows, raw_policy_rows, policy_errors = _parse_shift_leave_policy_rows()
    else:
        try:
            existing_policy_rows = _policy_rows_from_models(_shift_leave_policies(shift.id))
        except (OperationalError, ProgrammingError, LookupError):
            db.session.rollback()
            current_app.logger.warning(
                "Shift leave policies lookup failed while editing shift.",
                exc_info=True,
            )
            flash("No se pudieron cargar vacaciones permisos del turno. Revisa migraciones pendientes.", "warning")

    if request.method == "GET":
        form.name.data = shift.name
        form.break_counts_as_worked_bool.data = shift.break_counts_as_worked_bool
        form.break_minutes.data = shift.break_minutes
        form.expected_hours.data = shift.expected_hours
        form.expected_hours_frequency.data = _enum_value(shift.expected_hours_frequency, fallback="DAILY")

    policy_rows_for_template = (
        raw_policy_rows
        if request.method == "POST" and raw_policy_rows
        else existing_policy_rows if existing_policy_rows else [_new_blank_policy_row()]
    )

    if form.validate_on_submit():
        shift_name = form.name.data.strip()
        if not shift_name:
            flash("El nombre del turno es obligatorio.", "danger")
            return render_template(
                "admin/shift_edit.html",
                form=form,
                shift=shift,
                policy_rows=policy_rows_for_template,
                policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                policy_default_valid_from=policy_default_valid_from,
                policy_default_valid_to=policy_default_valid_to,
            )
        if policy_errors:
            for message in policy_errors:
                flash(message, "danger")
            return render_template(
                "admin/shift_edit.html",
                form=form,
                shift=shift,
                policy_rows=policy_rows_for_template,
                policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                policy_default_valid_from=policy_default_valid_from,
                policy_default_valid_to=policy_default_valid_to,
            )

        try:
            existing_shift = db.session.execute(
                select(Shift).where(
                    Shift.tenant_id == tenant_id,
                    Shift.name == shift_name,
                    Shift.id != shift.id,
                )
            ).scalar_one_or_none()
            if existing_shift is not None:
                flash("Ya existe un turno con ese nombre.", "warning")
                return render_template(
                    "admin/shift_edit.html",
                    form=form,
                    shift=shift,
                    policy_rows=policy_rows_for_template,
                    policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
                    policy_default_valid_from=policy_default_valid_from,
                    policy_default_valid_to=policy_default_valid_to,
                )

            previous_payload = {
                "name": shift.name,
                "break_counts_as_worked_bool": shift.break_counts_as_worked_bool,
                "break_minutes": shift.break_minutes,
                "expected_hours": str(shift.expected_hours),
                "expected_hours_frequency": _enum_value(shift.expected_hours_frequency),
            }

            shift.name = shift_name
            shift.break_counts_as_worked_bool = bool(form.break_counts_as_worked_bool.data)
            shift.break_minutes = int(form.break_minutes.data)
            shift.expected_hours = form.expected_hours.data
            shift.expected_hours_frequency = ExpectedHoursFrequency(form.expected_hours_frequency.data)
            before_policy_payload, after_policy_payload = _replace_shift_leave_policies(tenant_id, shift.id, parsed_policy_rows)
            db.session.flush()
            log_audit(
                action="SHIFT_UPDATED",
                entity_type="shifts",
                entity_id=shift.id,
                payload={
                    "before": previous_payload,
                    "after": {
                        "name": shift.name,
                        "break_counts_as_worked_bool": shift.break_counts_as_worked_bool,
                        "break_minutes": shift.break_minutes,
                        "expected_hours": str(shift.expected_hours),
                        "expected_hours_frequency": shift.expected_hours_frequency.value,
                    },
                    "leave_policies": {
                        "before": before_policy_payload,
                        "after": after_policy_payload,
                    },
                },
            )
            db.session.commit()
            flash("Turno actualizado.", "success")
            return redirect(url_for("admin.shifts"))
        except (OperationalError, ProgrammingError, LookupError):
            db.session.rollback()
            current_app.logger.warning(
                "Shift update failed. Database schema likely out of date.",
                exc_info=True,
            )
            flash("No se pudo actualizar el turno. Revisa migraciones pendientes (alembic upgrade head).", "danger")

    return render_template(
        "admin/shift_edit.html",
        form=form,
        shift=shift,
        policy_rows=policy_rows_for_template,
        policy_unit_choices=LEAVE_POLICY_UNIT_CHOICES,
        policy_default_valid_from=policy_default_valid_from,
        policy_default_valid_to=policy_default_valid_to,
    )


@bp.get("/admin/approvals")
@login_required
@tenant_required
@approve_leaves_required
def approvals():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")
    actor_user_id = UUID(current_user.get_id())

    leave_stmt = (
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(
            LeaveRequest.tenant_id == tenant_id,
            LeaveRequest.status == LeaveRequestStatus.REQUESTED,
        )
        .order_by(LeaveRequest.created_at.asc())
    )
    leave_rows = db.session.execute(leave_stmt).all()

    correction_stmt = (
        select(PunchCorrectionRequest, Employee, TimeEvent)
        .join(Employee, Employee.id == PunchCorrectionRequest.employee_id)
        .join(TimeEvent, TimeEvent.id == PunchCorrectionRequest.source_event_id)
        .where(
            PunchCorrectionRequest.tenant_id == tenant_id,
            PunchCorrectionRequest.status == PunchCorrectionStatus.REQUESTED,
            or_(
                PunchCorrectionRequest.target_approver_user_id.is_(None),
                PunchCorrectionRequest.target_approver_user_id == actor_user_id,
            ),
        )
        .order_by(PunchCorrectionRequest.created_at.asc())
    )
    correction_rows = db.session.execute(correction_stmt).all()

    return render_template(
        "admin/approvals.html",
        rows=leave_rows,
        correction_rows=correction_rows,
        punch_correction_status_labels=PUNCH_CORRECTION_STATUS_LABELS,
    )


def _decide_leave(leave_request_id: UUID, status: LeaveRequestStatus):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    leave_request = db.session.execute(
        select(LeaveRequest).where(
            LeaveRequest.id == leave_request_id,
            LeaveRequest.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if leave_request is None:
        abort(404)
    if leave_request.status != LeaveRequestStatus.REQUESTED:
        abort(409, description="La solicitud ya fue decidida.")

    leave_request.status = status
    leave_request.approver_user_id = UUID(current_user.get_id())
    leave_request.decided_at = datetime.now(timezone.utc)
    log_audit(
        action=f"LEAVE_{status.value}",
        entity_type="leave_requests",
        entity_id=leave_request.id,
        payload={
            "employee_id": str(leave_request.employee_id),
            "type_id": str(leave_request.type_id),
            "leave_policy_id": str(leave_request.leave_policy_id) if leave_request.leave_policy_id else None,
            "date_from": leave_request.date_from.isoformat(),
            "date_to": leave_request.date_to.isoformat(),
            "minutes": leave_request.minutes,
            "status": leave_request.status.value,
        },
    )
    db.session.commit()
    status_labels = {
        LeaveRequestStatus.APPROVED: "aprobada",
        LeaveRequestStatus.REJECTED: "rechazada",
    }
    flash(f"Solicitud {status_labels.get(status, status.value.lower())}.", "success")
    return redirect(url_for("admin.approvals"))


@bp.post("/admin/approvals/<uuid:leave_request_id>/approve")
@login_required
@tenant_required
@approve_leaves_required
def approval_approve(leave_request_id: UUID):
    return _decide_leave(leave_request_id, LeaveRequestStatus.APPROVED)


@bp.post("/admin/approvals/<uuid:leave_request_id>/reject")
@login_required
@tenant_required
@approve_leaves_required
def approval_reject(leave_request_id: UUID):
    return _decide_leave(leave_request_id, LeaveRequestStatus.REJECTED)


@bp.get("/admin/punch-corrections")
@login_required
@tenant_required
@approve_punch_corrections_required
def punch_corrections():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")
    actor_user_id = UUID(current_user.get_id())

    pending_stmt = (
        select(PunchCorrectionRequest, Employee, TimeEvent)
        .join(Employee, Employee.id == PunchCorrectionRequest.employee_id)
        .join(TimeEvent, TimeEvent.id == PunchCorrectionRequest.source_event_id)
        .where(
            PunchCorrectionRequest.tenant_id == tenant_id,
            PunchCorrectionRequest.status == PunchCorrectionStatus.REQUESTED,
            or_(
                PunchCorrectionRequest.target_approver_user_id.is_(None),
                PunchCorrectionRequest.target_approver_user_id == actor_user_id,
            ),
        )
        .order_by(PunchCorrectionRequest.created_at.asc())
    )
    pending_rows = db.session.execute(pending_stmt).all()

    history_stmt = (
        select(PunchCorrectionRequest, Employee, TimeEvent)
        .join(Employee, Employee.id == PunchCorrectionRequest.employee_id)
        .join(TimeEvent, TimeEvent.id == PunchCorrectionRequest.source_event_id)
        .where(
            PunchCorrectionRequest.tenant_id == tenant_id,
            PunchCorrectionRequest.status.in_(
                (
                    PunchCorrectionStatus.APPROVED,
                    PunchCorrectionStatus.REJECTED,
                    PunchCorrectionStatus.CANCELLED,
                )
            ),
        )
        .order_by(PunchCorrectionRequest.created_at.desc())
        .limit(100)
    )
    history_rows = db.session.execute(history_stmt).all()
    return render_template(
        "admin/punch_corrections.html",
        pending_rows=pending_rows,
        history_rows=history_rows,
        punch_correction_status_labels=PUNCH_CORRECTION_STATUS_LABELS,
    )


def _decide_punch_correction(correction_request_id: UUID, status: PunchCorrectionStatus):
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    actor_user_id = UUID(current_user.get_id())
    correction_request = db.session.execute(
        select(PunchCorrectionRequest).where(
            PunchCorrectionRequest.id == correction_request_id,
            PunchCorrectionRequest.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if correction_request is None:
        abort(404)
    if correction_request.status != PunchCorrectionStatus.REQUESTED:
        abort(409, description="La solicitud ya fue decidida.")
    if (
        correction_request.target_approver_user_id is not None
        and correction_request.target_approver_user_id != actor_user_id
    ):
        abort(403, description="No puedes decidir esta solicitud.")

    source_event = db.session.execute(
        select(TimeEvent).where(
            TimeEvent.id == correction_request.source_event_id,
            TimeEvent.tenant_id == tenant_id,
            TimeEvent.employee_id == correction_request.employee_id,
        )
    ).scalar_one_or_none()
    if source_event is None:
        abort(404)

    already_superseded = db.session.execute(
        select(TimeEventSupersession).where(TimeEventSupersession.original_event_id == source_event.id)
    ).scalar_one_or_none()
    if already_superseded is not None:
        abort(409, description="El fichaje ya fue rectificado.")

    replacement_event_id: UUID | None = None
    if status == PunchCorrectionStatus.APPROVED:
        replacement_event = TimeEvent(
            tenant_id=tenant_id,
            employee_id=correction_request.employee_id,
            ts=correction_request.requested_ts,
            type=correction_request.requested_type,
            source=TimeEventSource.WEB,
            meta_json={
                "manual": True,
                "via": "punch_correction_approved",
                "source_event_id": str(source_event.id),
                "correction_request_id": str(correction_request.id),
            },
        )
        db.session.add(replacement_event)
        db.session.flush()
        replacement_event_id = replacement_event.id
        db.session.add(
            TimeEventSupersession(
                tenant_id=tenant_id,
                original_event_id=source_event.id,
                replacement_event_id=replacement_event.id,
                correction_request_id=correction_request.id,
            )
        )

    correction_request.status = status
    correction_request.approver_user_id = actor_user_id
    correction_request.applied_event_id = replacement_event_id
    correction_request.decided_at = datetime.now(timezone.utc)
    log_audit(
        action=f"PUNCH_CORRECTION_{status.value}",
        entity_type="punch_correction_requests",
        entity_id=correction_request.id,
        payload={
            "employee_id": str(correction_request.employee_id),
            "source_event_id": str(correction_request.source_event_id),
            "requested_ts": correction_request.requested_ts.isoformat(),
            "requested_type": correction_request.requested_type.value,
            "status": correction_request.status.value,
            "target_approver_user_id": (
                str(correction_request.target_approver_user_id)
                if correction_request.target_approver_user_id
                else None
            ),
            "applied_event_id": str(replacement_event_id) if replacement_event_id else None,
        },
    )
    db.session.commit()
    status_labels = {
        PunchCorrectionStatus.APPROVED: "aprobada",
        PunchCorrectionStatus.REJECTED: "rechazada",
    }
    flash(f"Solicitud de rectificacion {status_labels.get(status, status.value.lower())}.", "success")
    return redirect(url_for("admin.punch_corrections"))


@bp.post("/admin/punch-corrections/<uuid:correction_request_id>/approve")
@login_required
@tenant_required
@approve_punch_corrections_required
def punch_correction_approve(correction_request_id: UUID):
    return _decide_punch_correction(correction_request_id, PunchCorrectionStatus.APPROVED)


@bp.post("/admin/punch-corrections/<uuid:correction_request_id>/reject")
@login_required
@tenant_required
@approve_punch_corrections_required
def punch_correction_reject(correction_request_id: UUID):
    return _decide_punch_correction(correction_request_id, PunchCorrectionStatus.REJECTED)


@bp.get("/admin/reports/payroll")
@login_required
@tenant_required
@export_payroll_required
def payroll_report():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    form = AttendanceReportForm()
    form.employee_id.choices = [("", "Todos los empleados")] + _employee_choices(tenant_id)
    return render_template("admin/payroll.html", form=form)


@bp.post("/admin/reports/payroll/export")
@login_required
@tenant_required
@export_payroll_required
def payroll_export():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    form = AttendanceReportForm()
    form.employee_id.choices = [("", "Todos los empleados")] + _employee_choices(tenant_id)
    if not form.validate_on_submit():
        flash("Revisa los datos del reporte.", "danger")
        return render_template("admin/payroll.html", form=form), 400

    report_type = (form.report_type.data or "").strip().lower()
    output_format = (form.output_format.data or "").strip().lower()
    if report_type not in REPORT_TYPE_LABELS:
        abort(400, description="Tipo de reporte invalido.")
    if output_format not in REPORT_FORMAT_CONTENT_TYPES:
        abort(400, description="Formato de reporte invalido.")

    raw_employee_id = (form.employee_id.data or "").strip()
    selected_employee_id: UUID | None = None
    if raw_employee_id:
        try:
            selected_employee_id = UUID(raw_employee_id)
        except ValueError:
            abort(400, description="Empleado invalido.")

    employees = _attendance_report_employees(tenant_id, selected_employee_id)
    if selected_employee_id is not None and not employees:
        abort(404, description="Empleado no encontrado para el tenant actual.")

    start_utc, end_utc = _report_window_utc(form.date_from.data, form.date_to.data)
    event_rows = _attendance_report_events(tenant_id, start_utc, end_utc, selected_employee_id)

    if report_type == "control":
        headers, rows = _build_control_report_rows(event_rows)
    else:
        headers, rows = _build_executive_report_rows(employees, event_rows)

    report_title = f"{REPORT_TYPE_LABELS[report_type]} ({form.date_from.data} a {form.date_to.data})"
    if output_format == "csv":
        payload = to_csv_bytes(headers, rows)
    elif output_format == "xlsx":
        sheet_name = "ControlHorario" if report_type == "control" else "ResumenEjecutivo"
        payload = to_xlsx_bytes(headers, rows, sheet_name=sheet_name)
    elif output_format == "pdf":
        payload = to_pdf_bytes(report_title, headers, rows)
    else:
        payload = to_json_bytes(
            {
                "generated_at": datetime.now(timezone.utc),
                "tenant_id": str(tenant_id),
                "report_type": report_type,
                "report_label": REPORT_TYPE_LABELS[report_type],
                "date_from": form.date_from.data.isoformat(),
                "date_to": form.date_to.data.isoformat(),
                "timezone": str(_report_timezone()),
                "employee_id": str(selected_employee_id) if selected_employee_id is not None else None,
                "headers": headers,
                "rows": [dict(zip(headers, row)) for row in rows],
                "row_count": len(rows),
            }
        )

    response = make_response(payload)
    response.headers["Content-Type"] = REPORT_FORMAT_CONTENT_TYPES[output_format]
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{_report_download_filename(report_type, output_format, form.date_from.data, form.date_to.data, selected_employee_id)}"'
    )
    return response


@bp.get("/admin/adjustments")
@login_required
@tenant_required
@view_adjustments_required
def adjustments_stub():
    rows = list(
        db.session.execute(select(TimeAdjustment).order_by(TimeAdjustment.created_at.desc()).limit(20)).scalars().all()
    )
    return render_template("admin/adjustments.html", rows=rows)
