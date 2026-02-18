"""Admin routes."""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.audit import log_audit
from app.extensions import db
from app.forms import DateRangeExportForm, EmployeeCreateForm, EmployeeEditForm, ShiftCreateForm
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    MembershipRole,
    Shift,
    ShiftLeavePolicy,
    TimeAdjustment,
    TimeEvent,
)
from app.security import hash_secret
from app.tenant import get_active_tenant_id, roles_required, tenant_required


bp = Blueprint("admin", __name__)

ADMIN_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}
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


@bp.get("/admin/employees")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
def team_today():
    return redirect(url_for("admin.shifts"))


@bp.get("/admin/turnos")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
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
@roles_required(ADMIN_ROLES)
def approvals():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    stmt = (
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(
            LeaveRequest.tenant_id == tenant_id,
            LeaveRequest.status == LeaveRequestStatus.REQUESTED,
        )
        .order_by(LeaveRequest.created_at.asc())
    )
    rows = db.session.execute(stmt).all()
    return render_template("admin/approvals.html", rows=rows)


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
@roles_required(ADMIN_ROLES)
def approval_approve(leave_request_id: UUID):
    return _decide_leave(leave_request_id, LeaveRequestStatus.APPROVED)


@bp.post("/admin/approvals/<uuid:leave_request_id>/reject")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def approval_reject(leave_request_id: UUID):
    return _decide_leave(leave_request_id, LeaveRequestStatus.REJECTED)


@bp.get("/admin/reports/payroll")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def payroll_report():
    form = DateRangeExportForm()
    return render_template("admin/payroll.html", form=form)


@bp.post("/admin/reports/payroll/export")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def payroll_export():
    form = DateRangeExportForm()
    if not form.validate_on_submit():
        flash("Invalid date range.", "danger")
        return redirect(url_for("admin.payroll_report"))

    start = datetime.combine(form.date_from.data, time.min, tzinfo=timezone.utc)
    end = datetime.combine(form.date_to.data, time.max, tzinfo=timezone.utc)
    stmt = (
        select(TimeEvent, Employee)
        .join(Employee, Employee.id == TimeEvent.employee_id)
        .where(TimeEvent.ts >= start, TimeEvent.ts <= end)
        .order_by(TimeEvent.ts.asc())
    )
    rows = db.session.execute(stmt).all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["employee_id", "employee_name", "timestamp", "event_type", "source"])
    for event, employee in rows:
        writer.writerow([str(employee.id), employee.name, event.ts.isoformat(), event.type.value, event.source.value])

    response = make_response(out.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=payroll_export.csv"
    return response


@bp.get("/admin/adjustments")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def adjustments_stub():
    rows = list(
        db.session.execute(select(TimeAdjustment).order_by(TimeAdjustment.created_at.desc()).limit(20)).scalars().all()
    )
    return render_template("admin/adjustments.html", rows=rows)
