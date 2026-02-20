from __future__ import annotations

from datetime import date, datetime, timezone
import io
import re

from sqlalchemy import select

from app.blueprints.admin import _team_health_counts
from app.extensions import db
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    ImportJob,
    ImportJobStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Membership,
    MembershipRole,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
    Shift,
    Tenant,
    TimeEvent,
    TimeEventSource,
    TimeEventType,
    User,
)
from app.security import hash_secret


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _latest_import_job(app) -> ImportJob:
    with app.app_context():
        return db.session.execute(select(ImportJob).order_by(ImportJob.created_at.desc())).scalar_one()


def _assert_card_count(body: str, title: str, count: int) -> None:
    pattern = rf"{re.escape(title)}.*?>{count}<"
    assert re.search(pattern, body, re.DOTALL)


def test_bulk_preview_rejects_csv_without_name_column(admin_only_client):
    _login_admin(admin_only_client)

    response = admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(b"email,active\nuno@example.com,true\n"), "empleados.csv")},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "columna obligatoria" in body

    with admin_only_client.application.app_context():
        rows = list(db.session.execute(select(ImportJob)).scalars().all())
        assert rows == []


def test_bulk_preview_flags_duplicate_emails_and_invalid_boolean(admin_only_client):
    _login_admin(admin_only_client)

    create_shift = admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "8",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    assert create_shift.status_code == 302

    csv_payload = (
        "name,email,active,shift_name,create_user,role\n"
        "Ana,duplicado@example.com,siempre,General,true,EMPLOYEE\n"
        "Luis,duplicado@example.com,true,General,false,\n"
    ).encode("utf-8")
    response = admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(csv_payload), "empleados.csv")},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Email duplicado en el mismo CSV." in body
    assert "Valor invalido para" in body
    assert "active" in body

    with admin_only_client.application.app_context():
        import_job = db.session.execute(select(ImportJob).order_by(ImportJob.created_at.desc())).scalar_one()
        assert import_job.summary_json["total"] == 2
        assert import_job.summary_json["invalid"] == 2


def test_bulk_commit_creates_employee_without_user(admin_only_client):
    _login_admin(admin_only_client)

    csv_payload = (
        "name,email,active,shift_name,create_user,role\n"
        "Solo Empleado,,true,,false,\n"
    ).encode("utf-8")
    preview = admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(csv_payload), "empleados.csv")},
        follow_redirects=False,
    )
    assert preview.status_code == 302

    import_job = _latest_import_job(admin_only_client.application)
    commit = admin_only_client.post(
        "/admin/import/employees/commit",
        data={"import_job_id": str(import_job.id)},
        follow_redirects=False,
    )
    assert commit.status_code == 302

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.name == "Solo Empleado")).scalar_one()
        assert employee.email is None
        employee_memberships = list(
            db.session.execute(select(Membership).where(Membership.role == MembershipRole.EMPLOYEE)).scalars().all()
        )
        assert employee_memberships == []

        refreshed = db.session.get(ImportJob, import_job.id)
        assert refreshed is not None
        assert refreshed.status == ImportJobStatus.COMMITTED
        assert refreshed.summary_json["committed_employees"] == 1
        assert refreshed.summary_json["committed_users"] == 0


def test_bulk_commit_creates_user_membership_shift_and_credentials_csv(admin_only_client):
    _login_admin(admin_only_client)

    create_template_shift = admin_only_client.post(
        "/admin/turnos/template/oficina-8h",
        data={"next": "/admin/import/employees"},
        follow_redirects=False,
    )
    assert create_template_shift.status_code == 302

    csv_payload = (
        "name,email,active,shift_name,create_user,role\n"
        "Maria User,maria.user@example.com,true,Oficina 8h,true,EMPLOYEE\n"
    ).encode("utf-8")
    preview = admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(csv_payload), "empleados.csv")},
        follow_redirects=False,
    )
    assert preview.status_code == 302

    import_job = _latest_import_job(admin_only_client.application)
    commit = admin_only_client.post(
        "/admin/import/employees/commit",
        data={"import_job_id": str(import_job.id)},
        follow_redirects=False,
    )
    assert commit.status_code == 200
    assert ".csv" in commit.headers["Content-Disposition"]
    csv_body = commit.get_data(as_text=True)
    assert "maria.user@example.com" in csv_body
    assert "Maria User" in csv_body

    with admin_only_client.application.app_context():
        user = db.session.execute(select(User).where(User.email == "maria.user@example.com")).scalar_one()
        assert user.must_change_password is True

        membership = db.session.execute(select(Membership).where(Membership.user_id == user.id)).scalar_one()
        assert membership.role == MembershipRole.EMPLOYEE
        assert membership.employee_id is not None

        assignment = db.session.execute(
            select(EmployeeShiftAssignment).where(EmployeeShiftAssignment.employee_id == membership.employee_id)
        ).scalar_one()
        shift = db.session.get(Shift, assignment.shift_id)
        assert shift is not None
        assert shift.name == "Oficina 8h"

        refreshed = db.session.get(ImportJob, import_job.id)
        assert refreshed is not None
        assert refreshed.status == ImportJobStatus.COMMITTED


def test_bulk_commit_is_idempotent_and_second_attempt_returns_conflict(admin_only_client):
    _login_admin(admin_only_client)

    csv_payload = (
        "name,email,active,shift_name,create_user,role\n"
        "Empleado Dos,,true,,false,\n"
    ).encode("utf-8")
    preview = admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(csv_payload), "empleados.csv")},
        follow_redirects=False,
    )
    assert preview.status_code == 302

    import_job = _latest_import_job(admin_only_client.application)
    first_commit = admin_only_client.post(
        "/admin/import/employees/commit",
        data={"import_job_id": str(import_job.id)},
        follow_redirects=False,
    )
    assert first_commit.status_code == 302

    second_commit = admin_only_client.post(
        "/admin/import/employees/commit",
        data={"import_job_id": str(import_job.id)},
        follow_redirects=False,
    )
    assert second_commit.status_code == 409


def test_getting_started_progress_reflects_successful_import(admin_only_client):
    _login_admin(admin_only_client)

    admin_only_client.post(
        "/admin/turnos/template/oficina-8h",
        data={"next": "/admin/getting-started"},
        follow_redirects=False,
    )

    csv_payload = (
        "name,email,active,shift_name,create_user,role\n"
        "Onboarding User,onboarding.user@example.com,true,Oficina 8h,true,EMPLOYEE\n"
    ).encode("utf-8")
    admin_only_client.post(
        "/admin/import/employees/preview",
        data={"csv_file": (io.BytesIO(csv_payload), "empleados.csv")},
        follow_redirects=False,
    )
    import_job = _latest_import_job(admin_only_client.application)
    admin_only_client.post(
        "/admin/import/employees/commit",
        data={"import_job_id": str(import_job.id)},
        follow_redirects=False,
    )

    response = admin_only_client.get("/admin/getting-started", follow_redirects=False)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Progreso:</strong> 4/5 (80%)" in body


def test_team_health_shows_expected_counts_and_action_links(admin_only_client):
    _login_admin(admin_only_client)

    with admin_only_client.application.app_context():
        tenant_id = db.session.execute(
            select(Membership.tenant_id).where(Membership.role == MembershipRole.ADMIN)
        ).scalar_one()
        tenant = db.session.get(Tenant, tenant_id)
        assert tenant is not None

        shift = Shift(
            tenant_id=tenant.id,
            name="Turno Base",
            break_counts_as_worked_bool=True,
            break_minutes=30,
            expected_hours=8,
            expected_hours_frequency=ExpectedHoursFrequency.DAILY,
        )
        db.session.add(shift)
        db.session.flush()

        emp_a = Employee(tenant_id=tenant.id, name="Emp A", email="emp.a@example.com", active=True)
        emp_b = Employee(tenant_id=tenant.id, name="Emp B", email="emp.b@example.com", active=True)
        emp_c = Employee(tenant_id=tenant.id, name="Emp C", email="emp.c@example.com", active=True)
        db.session.add_all([emp_a, emp_b, emp_c])
        db.session.flush()

        linked_user = User(email="linked.employee@example.com", password_hash=hash_secret("password123"), is_active=True)
        orphan_user = User(email="orphan.employee@example.com", password_hash=hash_secret("password123"), is_active=True)
        db.session.add_all([linked_user, orphan_user])
        db.session.flush()

        db.session.add_all(
            [
                Membership(
                    tenant_id=tenant.id,
                    user_id=linked_user.id,
                    role=MembershipRole.EMPLOYEE,
                    employee_id=emp_b.id,
                ),
                Membership(
                    tenant_id=tenant.id,
                    user_id=orphan_user.id,
                    role=MembershipRole.EMPLOYEE,
                    employee_id=None,
                ),
            ]
        )

        db.session.add(
            EmployeeShiftAssignment(
                tenant_id=tenant.id,
                employee_id=emp_c.id,
                shift_id=shift.id,
                effective_from=date.today(),
                effective_to=None,
            )
        )

        event = TimeEvent(
            tenant_id=tenant.id,
            employee_id=emp_c.id,
            ts=datetime.now(timezone.utc),
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
        )
        db.session.add(event)
        db.session.flush()

        db.session.add(
            PunchCorrectionRequest(
                tenant_id=tenant.id,
                employee_id=emp_c.id,
                source_event_id=event.id,
                requested_ts=event.ts,
                requested_type=TimeEventType.IN,
                reason="Rectificacion pendiente para validar panel de salud.",
                status=PunchCorrectionStatus.REQUESTED,
            )
        )

        leave_type = LeaveType(
            tenant_id=tenant.id,
            code="VAC",
            name="Vacaciones",
            paid_bool=True,
            requires_approval_bool=True,
            counts_as_worked_bool=False,
        )
        db.session.add(leave_type)
        db.session.flush()
        db.session.add(
            LeaveRequest(
                tenant_id=tenant.id,
                employee_id=emp_a.id,
                type_id=leave_type.id,
                leave_policy_id=None,
                date_from=date.today(),
                date_to=date.today(),
                reason="Permiso pendiente para validar panel.",
                status=LeaveRequestStatus.REQUESTED,
            )
        )
        db.session.commit()

        counts = _team_health_counts(tenant.id)
        assert counts == {
            "employees_without_user": 2,
            "employee_users_without_employee": 1,
            "employees_without_shift": 2,
            "active_without_events_7d": 2,
            "pending_punch_corrections": 1,
            "pending_leave_requests": 1,
        }

    response = admin_only_client.get("/admin/team-health", follow_redirects=False)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "/admin/employees?filter=without-user" in body
    assert "/admin/users?filter=employee-without-link" in body
    assert "/admin/employees?filter=without-shift" in body
    assert "/admin/employees?filter=without-events-7d" in body
    _assert_card_count(body, "Empleados sin usuario", 2)
    _assert_card_count(body, "Usuarios EMPLOYEE sin empleado", 1)
    _assert_card_count(body, "Empleados activos sin turno", 2)
    _assert_card_count(body, "Activos sin fichajes (7 dias)", 2)
    _assert_card_count(body, "Rectificaciones pendientes", 1)
    _assert_card_count(body, "Permisos pendientes", 1)
