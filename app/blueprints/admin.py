from __future__ import annotations

import csv
import io
from datetime import datetime, time, timezone
from uuid import UUID

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from app.audit import log_audit
from app.extensions import db
from app.forms import DateRangeExportForm, EmployeeCreateForm, EmployeeEditForm, ShiftForm
from app.models import (
    Employee,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    MembershipRole,
    Shift,
    ShiftPeriod,
    TimeAdjustment,
    TimeEvent,
)
from app.security import hash_secret
from app.tenant import get_active_tenant_id, roles_required, tenant_required


bp = Blueprint("admin", __name__)

ADMIN_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}


def _shift_choices(tenant_id):
    shifts = list(db.session.execute(select(Shift).where(Shift.tenant_id == tenant_id).order_by(Shift.name.asc())).scalars().all())
    return [("", "Sin turno")] + [(str(shift.id), shift.name) for shift in shifts]


@bp.get("/admin/employees")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def employees_list():
    active_employees = list(
        db.session.execute(select(Employee).where(Employee.active.is_(True)).order_by(Employee.name.asc())).scalars().all()
    )
    inactive_employees = list(
        db.session.execute(select(Employee).where(Employee.active.is_(False)).order_by(Employee.name.asc())).scalars().all()
    )
    return render_template("admin/employees.html", active_employees=active_employees, inactive_employees=inactive_employees)


@bp.route("/admin/employees/new", methods=["GET", "POST"])
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def employees_new():
    form = EmployeeCreateForm()
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    form.shift_id.choices = _shift_choices(tenant_id)

    if form.validate_on_submit():
        employee = Employee(
            tenant_id=tenant_id,
            name=form.name.data.strip(),
            email=form.email.data.strip().lower() if form.email.data else None,
            pin_hash=hash_secret(form.pin.data) if form.pin.data else None,
            active=form.active.data,
            shift_id=UUID(form.shift_id.data) if form.shift_id.data else None,
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
def employee_edit(employee_id: UUID):
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        abort(404)

    form = EmployeeEditForm(obj=employee)
    form.shift_id.choices = _shift_choices(employee.tenant_id)

    if request.method == "GET":
        form.shift_id.data = str(employee.shift_id) if employee.shift_id else ""

    if form.validate_on_submit():
        employee.name = form.name.data.strip()
        employee.email = form.email.data.strip().lower() if form.email.data else None
        if form.pin.data:
            employee.pin_hash = hash_secret(form.pin.data)
        employee.shift_id = UUID(form.shift_id.data) if form.shift_id.data else None
        log_audit(
            action="EMPLOYEE_UPDATED",
            entity_type="employees",
            entity_id=employee.id,
            payload={"name": employee.name, "email": employee.email},
        )
        db.session.commit()
        flash("Employee updated.", "success")
        return redirect(url_for("admin.employees_list"))

    return render_template("admin/employee_edit.html", form=form, employee=employee)


@bp.post("/admin/employees/<uuid:employee_id>/deactivate")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def employee_deactivate(employee_id: UUID):
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        abort(404)

    employee.active = False
    log_audit(action="EMPLOYEE_DEACTIVATED", entity_type="employees", entity_id=employee.id, payload={"active": False})
    db.session.commit()
    flash("Employee deactivated.", "success")
    return redirect(url_for("admin.employees_list"))


@bp.route("/admin/employees/<uuid:employee_id>/delete", methods=["GET", "POST"])
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def employee_delete(employee_id: UUID):
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        abort(404)

    if request.method == "POST":
        db.session.delete(employee)
        log_audit(action="EMPLOYEE_DELETED", entity_type="employees", entity_id=employee.id, payload={"name": employee.name})
        db.session.commit()
        flash("Employee deleted.", "success")
        return redirect(url_for("admin.employees_list"))

    return render_template("admin/employee_delete_confirm.html", employee=employee)


@bp.route("/admin/shifts", methods=["GET", "POST"])
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def shifts():
    tenant_id = get_active_tenant_id()
    if tenant_id is None:
        abort(400, description="No active tenant selected.")

    form = ShiftForm()
    if form.validate_on_submit():
        shift = Shift(
            tenant_id=tenant_id,
            name=form.name.data.strip(),
            break_counts_as_work=form.break_counts_as_work.data,
            break_minutes=max(0, form.break_minutes.data or 0),
            expected_hours=max(0, form.expected_hours.data or 0),
            expected_hours_period=ShiftPeriod(form.expected_hours_period.data),
        )
        db.session.add(shift)
        db.session.flush()
        log_audit(
            action="SHIFT_CREATED",
            entity_type="shifts",
            entity_id=shift.id,
            payload={"name": shift.name, "period": shift.expected_hours_period.value},
        )
        db.session.commit()
        flash("Shift created.", "success")
        return redirect(url_for("admin.shifts"))

    shifts_rows = list(db.session.execute(select(Shift).where(Shift.tenant_id == tenant_id).order_by(Shift.name.asc())).scalars().all())
    return render_template("admin/shifts.html", form=form, shifts=shifts_rows)


@bp.get("/admin/team-today")
@login_required
@tenant_required
@roles_required(ADMIN_ROLES)
def team_today():
    start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    end = datetime.combine(datetime.now(timezone.utc).date(), time.max, tzinfo=timezone.utc)

    employees = list(db.session.execute(select(Employee).where(Employee.active.is_(True)).order_by(Employee.name.asc())).scalars().all())
    rows = []
    for employee in employees:
        stmt = (
            select(TimeEvent)
            .where(TimeEvent.employee_id == employee.id, TimeEvent.ts >= start, TimeEvent.ts <= end)
            .order_by(TimeEvent.ts.desc())
            .limit(1)
        )
        last_event = db.session.execute(stmt).scalar_one_or_none()
        rows.append((employee, last_event))
    return render_template("admin/team_today.html", rows=rows)


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
