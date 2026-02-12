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
from app.forms import DateRangeExportForm, EmployeeCreateForm
from app.models import Employee, LeaveRequest, LeaveRequestStatus, LeaveType, MembershipRole, TimeAdjustment, TimeEvent
from app.security import hash_secret
from app.tenant import get_active_tenant_id, roles_required, tenant_required


bp = Blueprint("admin", __name__)

ADMIN_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}


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

