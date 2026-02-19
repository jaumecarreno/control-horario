from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models import Employee, Membership, MembershipRole, User


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _create_employee(admin_only_client, name: str, email: str):
    response = admin_only_client.post(
        "/admin/employees/new",
        data={"name": name, "email": email, "pin": "1234", "active": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_admin_can_create_employee_user(admin_only_client):
    _login_admin(admin_only_client)
    _create_employee(admin_only_client, "Empleado Usuario", "empleado.usuario@example.com")

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "empleado.usuario@example.com")).scalar_one()

    response = admin_only_client.post(
        "/admin/users/new",
        data={
            "email": "  Nuevo.Usuario@Example.com ",
            "password": "password123",
            "confirm_password": "password123",
            "role": "EMPLOYEE",
            "employee_id": str(employee.id),
            "active": "y",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with admin_only_client.application.app_context():
        created_user = db.session.execute(select(User).where(User.email == "nuevo.usuario@example.com")).scalar_one()
        assert created_user.is_active is True
        membership = db.session.execute(select(Membership).where(Membership.user_id == created_user.id)).scalar_one()
        assert membership.role == MembershipRole.EMPLOYEE
        assert membership.employee_id == employee.id


def test_admin_role_requires_empty_employee(admin_only_client):
    _login_admin(admin_only_client)
    _create_employee(admin_only_client, "Empleado Admin", "empleado.admin@example.com")

    with admin_only_client.application.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "empleado.admin@example.com")).scalar_one()

    response = admin_only_client.post(
        "/admin/users/new",
        data={
            "email": "admin.extra@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "role": "ADMIN",
            "employee_id": str(employee.id),
            "active": "y",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "no deben tener empleado asociado" in response.get_data(as_text=True)

    with admin_only_client.application.app_context():
        assert db.session.execute(select(User).where(User.email == "admin.extra@example.com")).scalar_one_or_none() is None
