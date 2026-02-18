from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    AuditLog,
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Shift,
    ShiftLeavePolicy,
    Tenant,
)


def _login_owner(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _select_tenant(client, slug: str):
    tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one()
    with client.session_transaction() as session:
        session["active_tenant_id"] = str(tenant_id)


def _create_leave_policy_for_owner(
    *,
    unit: LeavePolicyUnit = LeavePolicyUnit.DAYS,
    amount: Decimal = Decimal("22"),
    valid_from: date = date(2026, 1, 1),
    valid_to: date = date(2026, 12, 31),
) -> tuple[Employee, LeaveType, ShiftLeavePolicy]:
    employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
    tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()

    shift = Shift(
        tenant_id=tenant_id,
        name="General",
        break_counts_as_worked_bool=True,
        break_minutes=30,
        expected_hours=Decimal("7.50"),
        expected_hours_frequency=ExpectedHoursFrequency.DAILY,
    )
    leave_type = LeaveType(
        tenant_id=tenant_id,
        code="VACACIONES",
        name="Vacaciones",
        paid_bool=False,
        requires_approval_bool=True,
        counts_as_worked_bool=False,
    )
    db.session.add_all([shift, leave_type])
    db.session.flush()
    db.session.add(
        EmployeeShiftAssignment(
            tenant_id=tenant_id,
            employee_id=employee.id,
            shift_id=shift.id,
            effective_from=date(2020, 1, 1),
            effective_to=None,
        )
    )
    policy = ShiftLeavePolicy(
        tenant_id=tenant_id,
        shift_id=shift.id,
        leave_type_id=leave_type.id,
        name="Vacaciones",
        amount=amount,
        unit=unit,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    db.session.add(policy)
    db.session.commit()
    return employee, leave_type, policy


def test_me_leaves_creates_requested_leave_and_audit_record(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee, leave_type, policy = _create_leave_policy_for_owner()
        employee_id = employee.id
        leave_type_id = leave_type.id
        policy_id = policy.id

    submit = client.post(
        "/me/leaves",
        data={
            "type_id": str(policy_id),
            "date_from": "2026-02-10",
            "date_to": "2026-02-12",
            "minutes": "",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    html = submit.get_data(as_text=True)
    assert "Solicitud registrada." in html

    with app.app_context():
        leave_request = db.session.execute(
            select(LeaveRequest).where(LeaveRequest.employee_id == employee_id)
        ).scalar_one()
        assert leave_request.status == LeaveRequestStatus.REQUESTED
        assert leave_request.type_id == leave_type_id
        assert leave_request.leave_policy_id == policy_id

        audit = db.session.execute(
            select(AuditLog).where(AuditLog.action == "LEAVE_REQUESTED").order_by(AuditLog.ts.desc())
        ).scalar_one()
        assert audit.payload_json["employee_id"] == str(employee_id)
        assert audit.payload_json["leave_policy_id"] == str(policy_id)
        assert audit.payload_json["status"] == LeaveRequestStatus.REQUESTED.value


def test_me_leaves_requires_minutes_for_hour_policies(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        _, _, policy = _create_leave_policy_for_owner(
            unit=LeavePolicyUnit.HOURS,
            amount=Decimal("40"),
        )
        policy_id = policy.id

    submit = client.post(
        "/me/leaves",
        data={
            "type_id": str(policy_id),
            "date_from": "2026-02-10",
            "date_to": "2026-02-10",
            "minutes": "",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    html = submit.get_data(as_text=True)
    assert "Para permisos en horas debes indicar minutos mayores que cero." in html


def test_me_leaves_rejects_dates_outside_policy_range(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        _, _, policy = _create_leave_policy_for_owner(
            valid_from=date(2026, 2, 1),
            valid_to=date(2026, 3, 31),
        )
        policy_id = policy.id

    submit = client.post(
        "/me/leaves",
        data={
            "type_id": str(policy_id),
            "date_from": "2026-04-05",
            "date_to": "2026-04-06",
            "minutes": "",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    html = submit.get_data(as_text=True)
    assert "Las fechas solicitadas estan fuera del rango permitido para esta bolsa." in html


def test_me_leaves_rejects_when_pending_plus_requested_exceeds_balance(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee, leave_type, policy = _create_leave_policy_for_owner(amount=Decimal("5"))
        db.session.add(
            LeaveRequest(
                tenant_id=employee.tenant_id,
                employee_id=employee.id,
                type_id=leave_type.id,
                leave_policy_id=policy.id,
                date_from=date(2026, 2, 1),
                date_to=date(2026, 2, 4),
                minutes=None,
                status=LeaveRequestStatus.REQUESTED,
            )
        )
        db.session.commit()
        policy_id = policy.id
        employee_id = employee.id

    submit = client.post(
        "/me/leaves",
        data={
            "type_id": str(policy_id),
            "date_from": "2026-02-10",
            "date_to": "2026-02-11",
            "minutes": "",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    html = submit.get_data(as_text=True)
    assert "No hay saldo suficiente en esta bolsa para esa solicitud." in html

    with app.app_context():
        total = db.session.execute(
            select(func.count()).select_from(LeaveRequest).where(LeaveRequest.employee_id == employee_id)
        ).scalar_one()
        assert total == 1


def test_me_leaves_rejects_overlapping_active_requests(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee, leave_type, policy = _create_leave_policy_for_owner(amount=Decimal("30"))
        db.session.add(
            LeaveRequest(
                tenant_id=employee.tenant_id,
                employee_id=employee.id,
                type_id=leave_type.id,
                leave_policy_id=policy.id,
                date_from=date(2026, 2, 10),
                date_to=date(2026, 2, 12),
                minutes=None,
                status=LeaveRequestStatus.REQUESTED,
            )
        )
        db.session.commit()
        policy_id = policy.id
        employee_id = employee.id

    submit = client.post(
        "/me/leaves",
        data={
            "type_id": str(policy_id),
            "date_from": "2026-02-12",
            "date_to": "2026-02-14",
            "minutes": "",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    html = submit.get_data(as_text=True)
    assert "Ya existe una solicitud pendiente o aprobada que se solapa con estas fechas." in html

    with app.app_context():
        total = db.session.execute(
            select(func.count()).select_from(LeaveRequest).where(LeaveRequest.employee_id == employee_id)
        ).scalar_one()
        assert total == 1


def test_me_leave_cancel_changes_status_and_logs_audit(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee, leave_type, policy = _create_leave_policy_for_owner()
        leave_request = LeaveRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type_id=leave_type.id,
            leave_policy_id=policy.id,
            date_from=date(2026, 2, 20),
            date_to=date(2026, 2, 21),
            minutes=None,
            status=LeaveRequestStatus.REQUESTED,
        )
        db.session.add(leave_request)
        db.session.commit()
        leave_request_id = leave_request.id

    cancel = client.post(f"/me/leaves/{leave_request_id}/cancel", follow_redirects=True)
    assert cancel.status_code == 200
    html = cancel.get_data(as_text=True)
    assert "Solicitud cancelada." in html

    with app.app_context():
        refreshed = db.session.get(LeaveRequest, leave_request_id)
        assert refreshed is not None
        assert refreshed.status == LeaveRequestStatus.CANCELLED
        assert refreshed.decided_at is not None

        audit = db.session.execute(
            select(AuditLog).where(AuditLog.action == "LEAVE_CANCELLED").order_by(AuditLog.ts.desc())
        ).scalar_one()
        assert audit.payload_json["status"] == LeaveRequestStatus.CANCELLED.value


def test_me_leave_cancel_rejects_already_decided_requests(client, app):
    response = _login_owner(client)
    assert response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee, leave_type, policy = _create_leave_policy_for_owner()
        leave_request = LeaveRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type_id=leave_type.id,
            leave_policy_id=policy.id,
            date_from=date(2026, 2, 20),
            date_to=date(2026, 2, 21),
            minutes=None,
            status=LeaveRequestStatus.APPROVED,
        )
        db.session.add(leave_request)
        db.session.commit()
        leave_request_id = leave_request.id

    cancel = client.post(f"/me/leaves/{leave_request_id}/cancel", follow_redirects=False)
    assert cancel.status_code == 409

    with app.app_context():
        refreshed = db.session.get(LeaveRequest, leave_request_id)
        assert refreshed is not None
        assert refreshed.status == LeaveRequestStatus.APPROVED


def _create_admin_pending_leave_request(*, tenant_slug: str = "admin-tenant") -> LeaveRequest:
    tenant = db.session.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one()
    employee = Employee(
        tenant_id=tenant.id,
        name="Empleado Admin",
        email="empleado-admin@example.com",
        active=True,
    )
    leave_type = LeaveType(
        tenant_id=tenant.id,
        code="VACACIONES_ADMIN",
        name="Vacaciones",
        paid_bool=False,
        requires_approval_bool=True,
        counts_as_worked_bool=False,
    )
    db.session.add_all([employee, leave_type])
    db.session.flush()
    leave_request = LeaveRequest(
        tenant_id=tenant.id,
        employee_id=employee.id,
        type_id=leave_type.id,
        leave_policy_id=None,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 3),
        minutes=None,
        status=LeaveRequestStatus.REQUESTED,
    )
    db.session.add(leave_request)
    db.session.commit()
    return leave_request


def test_admin_approval_updates_status_and_audit(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        leave_request = _create_admin_pending_leave_request()
        leave_request_id = leave_request.id

    approve = admin_only_client.post(
        f"/admin/approvals/{leave_request_id}/approve",
        follow_redirects=True,
    )
    assert approve.status_code == 200
    html = approve.get_data(as_text=True)
    assert "Solicitud aprobada." in html

    with admin_only_client.application.app_context():
        refreshed = db.session.get(LeaveRequest, leave_request_id)
        assert refreshed is not None
        assert refreshed.status == LeaveRequestStatus.APPROVED
        assert refreshed.decided_at is not None
        assert refreshed.approver_user_id is not None

        audit = db.session.execute(
            select(AuditLog).where(AuditLog.action == "LEAVE_APPROVED").order_by(AuditLog.ts.desc())
        ).scalar_one()
        assert audit.payload_json["employee_id"] == str(refreshed.employee_id)
        assert audit.payload_json["status"] == LeaveRequestStatus.APPROVED.value


def test_admin_approval_redecide_returns_conflict(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        leave_request = _create_admin_pending_leave_request()
        leave_request_id = leave_request.id

    first = admin_only_client.post(f"/admin/approvals/{leave_request_id}/approve", follow_redirects=False)
    assert first.status_code == 302

    second = admin_only_client.post(f"/admin/approvals/{leave_request_id}/reject", follow_redirects=False)
    assert second.status_code == 409


def test_admin_cannot_decide_request_from_other_tenant(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        other_tenant = Tenant(name="Other Tenant", slug="other-tenant")
        db.session.add(other_tenant)
        db.session.commit()
        leave_request = _create_admin_pending_leave_request(tenant_slug="other-tenant")
        leave_request_id = leave_request.id

    response = admin_only_client.post(f"/admin/approvals/{leave_request_id}/approve", follow_redirects=False)
    assert response.status_code == 404
