"""Admin routes."""

from __future__ import annotations

import csv
import io
from datetime import datetime, time, timezone
from uuid import UUID

from flask import Blueprint, abort, flash, make_response, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from app.audit import log_audit
from app.extensions import db
from app.forms import DateRangeExportForm, EmployeeCreateForm, ShiftCreateForm
from app.models import (
    Employee,
    ExpectedHoursFrequency,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    MembershipRole,
    Shift,
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


@bp.get("/admin/employees")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def employees_list():
    employees = list(db.session.execute(select(Employee).order_by(Employee.name.asc())).scalars().all())
    return render_template("admin/employees.html", employees=employees)


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
    rows = list(db.session.execute(select(Shift).order_by(Shift.name.asc())).scalars().all())
    return render_template("admin/shifts.html", rows=rows, shift_frequency_labels=SHIFT_FREQUENCY_LABELS)


@bp.route("/admin/turnos/new", methods=["GET", "POST"])
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def shifts_new():
    form = ShiftCreateForm()
    if form.validate_on_submit():
        tenant_id = get_active_tenant_id()
        if tenant_id is None:
            abort(400, description="No active tenant selected.")

        shift_name = form.name.data.strip()
        if not shift_name:
            flash("El nombre del turno es obligatorio.", "danger")
            return render_template("admin/shift_new.html", form=form)

        existing_shift = db.session.execute(
            select(Shift).where(Shift.tenant_id == tenant_id, Shift.name == shift_name)
        ).scalar_one_or_none()
        if existing_shift is not None:
            flash("Ya existe un turno con ese nombre.", "warning")
            return render_template("admin/shift_new.html", form=form)

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
            },
        )
        db.session.commit()
        flash("Turno creado.", "success")
        return redirect(url_for("admin.shifts"))

    return render_template("admin/shift_new.html", form=form)


@bp.get("/admin/approvals")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def approvals():
    stmt = (
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.type_id)
        .where(LeaveRequest.status == LeaveRequestStatus.REQUESTED)
        .order_by(LeaveRequest.created_at.asc())
    )
    rows = db.session.execute(stmt).all()
    return render_template("admin/approvals.html", rows=rows)


def _decide_leave(leave_request_id: UUID, status: LeaveRequestStatus):
    leave_request = db.session.get(LeaveRequest, leave_request_id)
    if leave_request is None:
        abort(404)
    if leave_request.status != LeaveRequestStatus.REQUESTED:
        abort(409, description="Leave request already decided.")

    leave_request.status = status
    leave_request.approver_user_id = UUID(current_user.get_id())
    leave_request.decided_at = datetime.now(timezone.utc)
    log_audit(
        action=f"LEAVE_{status.value}",
        entity_type="leave_requests",
        entity_id=leave_request.id,
        payload={"status": status.value},
    )
    db.session.commit()
    flash(f"Leave request {status.value.lower()}.", "success")
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
