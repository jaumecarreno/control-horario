from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select, text

from app.extensions import db
from app.models import Employee, EmployeeShiftAssignment, Shift


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def test_admin_can_edit_employee_and_assign_shift(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    create_shift = admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "7.5",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    assert create_shift.status_code == 302

    create_employee = admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Uno",
            "email": "uno@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )
    assert create_employee.status_code == 302

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "uno@example.com")).scalar_one()
        shift = db.session.execute(select(Shift).where(Shift.name == "General")).scalar_one()
        employee_id = employee.id
        shift_id = shift.id

    update_employee = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Editado",
            "email": "editado@example.com",
            "pin": "",
            "assignment_shift_id": str(shift_id),
            "assignment_effective_from": "2026-02-01",
        },
        follow_redirects=True,
    )
    assert update_employee.status_code == 200
    body = update_employee.get_data(as_text=True)
    assert "Empleado actualizado." in body
    assert "Historial de turnos" in body

    with admin_only_client.application.app_context():
        updated_employee = db.session.get(Employee, employee_id)
        assert updated_employee is not None
        assert updated_employee.name == "Empleado Editado"
        assert updated_employee.email == "editado@example.com"

        assignment = db.session.execute(
            select(EmployeeShiftAssignment).where(EmployeeShiftAssignment.employee_id == employee_id)
        ).scalar_one()
        assert assignment.shift_id == shift_id
        assert assignment.effective_from == date(2026, 2, 1)
        assert assignment.effective_to is None


def test_admin_shift_reassignment_closes_previous_period(admin_only_client):
    _login_admin(admin_only_client)
    admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "7.5",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "Parcial",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "20",
            "expected_hours": "4",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Dos",
            "email": "dos@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "dos@example.com")).scalar_one()
        general = db.session.execute(select(Shift).where(Shift.name == "General")).scalar_one()
        parcial = db.session.execute(select(Shift).where(Shift.name == "Parcial")).scalar_one()
        employee_id = employee.id

    first_assignment = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Dos",
            "email": "dos@example.com",
            "pin": "",
            "active": "y",
            "assignment_shift_id": str(general.id),
            "assignment_effective_from": "2026-02-01",
        },
        follow_redirects=False,
    )
    assert first_assignment.status_code == 302

    second_assignment = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Dos",
            "email": "dos@example.com",
            "pin": "",
            "active": "y",
            "assignment_shift_id": str(parcial.id),
            "assignment_effective_from": "2026-02-16",
        },
        follow_redirects=False,
    )
    assert second_assignment.status_code == 302

    with admin_only_client.application.app_context():
        rows = list(
            db.session.execute(
                select(EmployeeShiftAssignment)
                .where(EmployeeShiftAssignment.employee_id == employee_id)
                .order_by(EmployeeShiftAssignment.effective_from.asc())
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].shift_id == general.id
        assert rows[0].effective_from == date(2026, 2, 1)
        assert rows[0].effective_to == date(2026, 2, 15)
        assert rows[1].shift_id == parcial.id
        assert rows[1].effective_from == date(2026, 2, 16)
        assert rows[1].effective_to is None


def test_admin_employee_edit_does_not_500_when_assignment_table_is_missing(admin_only_client):
    _login_admin(admin_only_client)
    admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "7.5",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Tres",
            "email": "tres@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "tres@example.com")).scalar_one()
        shift = db.session.execute(select(Shift).where(Shift.name == "General")).scalar_one()
        employee_id = employee.id
        shift_id = shift.id
        db.session.execute(text("DROP TABLE employee_shift_assignments"))
        db.session.commit()

    edit_page = admin_only_client.get(f"/admin/employees/{employee_id}/edit", follow_redirects=True)
    assert edit_page.status_code == 200
    assert "No se pudo cargar el historial de turnos del empleado." in edit_page.get_data(as_text=True)

    submit = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Tres",
            "email": "tres@example.com",
            "pin": "",
            "active": "y",
            "assignment_shift_id": str(shift_id),
            "assignment_effective_from": "2026-02-01",
        },
        follow_redirects=True,
    )
    assert submit.status_code == 200
    body = submit.get_data(as_text=True)
    assert "No se pudo actualizar turno. Revisa migraciones pendientes" in body


def test_employee_shift_history_hides_migration_sentinel_date(admin_only_client):
    _login_admin(admin_only_client)
    admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "7.5",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Cuatro",
            "email": "cuatro@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "cuatro@example.com")).scalar_one()
        shift = db.session.execute(select(Shift).where(Shift.name == "General")).scalar_one()
        employee_id = employee.id
        db.session.add(
            EmployeeShiftAssignment(
                tenant_id=employee.tenant_id,
                employee_id=employee.id,
                shift_id=shift.id,
                effective_from=date(1970, 1, 1),
                effective_to=None,
            )
        )
        db.session.commit()

    page = admin_only_client.get(f"/admin/employees/{employee_id}/edit", follow_redirects=True)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Asignacion inicial" in body
    assert "01/01/1970" not in body


def test_employees_list_groups_inactive_in_collapsible_section(admin_only_client):
    _login_admin(admin_only_client)
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Activo",
            "email": "activo@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Inactivo",
            "email": "inactivo@example.com",
            "pin": "1234",
        },
        follow_redirects=False,
    )

    with admin_only_client.application.app_context():
        inactive_employee = db.session.execute(select(Employee).where(Employee.email == "inactivo@example.com")).scalar_one()
        inactive_employee.active = False
        inactive_employee.active_status_changed_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
        db.session.commit()

    page = admin_only_client.get("/admin/employees", follow_redirects=True)
    assert page.status_code == 200
    body = page.get_data(as_text=True)

    assert "Mostrar inactivos (1)" in body
    assert "employee-row-inactive" in body

    active_pos = body.find("Empleado Activo")
    accordion_pos = body.find("Mostrar inactivos (1)")
    inactive_pos = body.find("Empleado Inactivo")
    assert active_pos != -1
    assert accordion_pos != -1
    assert inactive_pos != -1
    assert active_pos < accordion_pos < inactive_pos


def test_employee_active_status_changed_at_updates_when_active_flag_changes(admin_only_client):
    _login_admin(admin_only_client)
    admin_only_client.post(
        "/admin/employees/new",
        data={
            "name": "Empleado Estado",
            "email": "estado@example.com",
            "pin": "1234",
            "active": "y",
        },
        follow_redirects=False,
    )

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "estado@example.com")).scalar_one()
        employee_id = employee.id
        baseline = datetime(2020, 1, 1)
        employee.active_status_changed_at = baseline
        db.session.commit()

    same_status = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Estado",
            "email": "estado@example.com",
            "pin": "",
            "active": "y",
            "assignment_shift_id": "",
            "assignment_effective_from": "",
        },
        follow_redirects=False,
    )
    assert same_status.status_code == 302

    with admin_only_client.application.app_context():
        employee = db.session.get(Employee, employee_id)
        assert employee is not None
        assert employee.active is True
        assert employee.active_status_changed_at == baseline

    to_inactive = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Estado",
            "email": "estado@example.com",
            "pin": "",
            "assignment_shift_id": "",
            "assignment_effective_from": "",
        },
        follow_redirects=False,
    )
    assert to_inactive.status_code == 302

    with admin_only_client.application.app_context():
        employee = db.session.get(Employee, employee_id)
        assert employee is not None
        assert employee.active is False
        assert employee.active_status_changed_at > baseline
        inactive_changed_at = employee.active_status_changed_at

    to_active = admin_only_client.post(
        f"/admin/employees/{employee_id}/edit",
        data={
            "name": "Empleado Estado",
            "email": "estado@example.com",
            "pin": "",
            "active": "y",
            "assignment_shift_id": "",
            "assignment_effective_from": "",
        },
        follow_redirects=False,
    )
    assert to_active.status_code == 302

    with admin_only_client.application.app_context():
        employee = db.session.get(Employee, employee_id)
        assert employee is not None
        assert employee.active is True
        assert employee.active_status_changed_at > inactive_changed_at
