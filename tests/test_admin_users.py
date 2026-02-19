from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models import AuditLog, Employee, Membership, MembershipRole, Tenant, User


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



def _login_owner(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _activate_tenant(client, slug: str) -> None:
    with client.application.app_context():
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one()
    with client.session_transaction() as session:
        session["active_tenant_id"] = str(tenant_id)


def test_admin_can_edit_user_role_status_and_employee(admin_only_client):
    _login_admin(admin_only_client)
    _create_employee(admin_only_client, "Empleado Edit", "edit.user@example.com")
    _create_employee(admin_only_client, "Empleado Dos", "edit.user2@example.com")

    with admin_only_client.application.app_context():
        employee_one = db.session.execute(select(Employee).where(Employee.email == "edit.user@example.com")).scalar_one()
        employee_two = db.session.execute(select(Employee).where(Employee.email == "edit.user2@example.com")).scalar_one()

    create_response = admin_only_client.post(
        "/admin/users/new",
        data={
            "email": "editable@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "role": "EMPLOYEE",
            "employee_id": str(employee_one.id),
            "active": "y",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    with admin_only_client.application.app_context():
        editable_user = db.session.execute(select(User).where(User.email == "editable@example.com")).scalar_one()

    edit_response = admin_only_client.post(
        f"/admin/users/{editable_user.id}/edit",
        data={
            "role": "EMPLOYEE",
            "employee_id": str(employee_two.id),
        },
        follow_redirects=False,
    )
    assert edit_response.status_code == 302

    with admin_only_client.application.app_context():
        membership = db.session.execute(select(Membership).where(Membership.user_id == editable_user.id)).scalar_one()
        user = db.session.get(User, editable_user.id)
        assert membership.role == MembershipRole.EMPLOYEE
        assert membership.employee_id == employee_two.id
        assert user is not None
        assert user.is_active is False

        role_audit = db.session.execute(
            select(AuditLog).where(AuditLog.action == "USER_ROLE_CHANGED").order_by(AuditLog.ts.desc())
        ).scalar_one()
        assert role_audit.payload_json["before"]["employee_id"] == str(employee_one.id)
        assert role_audit.payload_json["after"]["employee_id"] == str(employee_two.id)

        status_audit = db.session.execute(
            select(AuditLog).where(AuditLog.action == "USER_STATUS_CHANGED").order_by(AuditLog.ts.desc())
        ).scalar_one()
        assert status_audit.payload_json["before"]["is_active"] is True
        assert status_audit.payload_json["after"]["is_active"] is False


def test_admin_cannot_change_owner_role(admin_only_client):
    _login_admin(admin_only_client)

    with admin_only_client.application.app_context():
        tenant_id = db.session.execute(select(Membership.tenant_id).where(Membership.role == MembershipRole.ADMIN)).scalar_one()
        owner_user = User(email="tenant.owner@example.com", password_hash="x", is_active=True)
        db.session.add(owner_user)
        db.session.flush()
        db.session.add(Membership(tenant_id=tenant_id, user_id=owner_user.id, role=MembershipRole.OWNER, employee_id=None))
        db.session.commit()
        owner_id = owner_user.id

    response = admin_only_client.post(
        f"/admin/users/{owner_id}/edit",
        data={"role": "ADMIN", "employee_id": "", "active": "y"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Solo OWNER puede cambiar asignaciones de OWNER" in response.get_data(as_text=True)

    with admin_only_client.application.app_context():
        membership = db.session.execute(select(Membership).where(Membership.user_id == owner_id)).scalar_one()
        assert membership.role == MembershipRole.OWNER


def test_user_cannot_remove_own_last_admin_access(client):
    login_response = _login_owner(client)
    assert login_response.status_code == 302
    _activate_tenant(client, "tenant-b")

    with client.application.app_context():
        tenant_b = db.session.execute(select(Tenant).where(Tenant.slug == "tenant-b")).scalar_one()
        owner_user = db.session.execute(select(User).where(User.email == "owner@example.com")).scalar_one()
        owner_user_id = owner_user.id
        employee = Employee(tenant_id=tenant_b.id, name="Owner Tenant B", email="owner.b@example.com", active=True)
        db.session.add(employee)
        db.session.commit()
        employee_id = employee.id

    response = client.post(
        f"/admin/users/{owner_user_id}/edit",
        data={"role": "EMPLOYEE", "employee_id": str(employee_id), "active": "y"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "No puedes quitar tu ultimo acceso administrativo del tenant" in response.get_data(as_text=True)

    with client.application.app_context():
        tenant_b_membership = db.session.execute(
            select(Membership).join(Tenant, Tenant.id == Membership.tenant_id).where(
                Membership.user_id == owner_user_id,
                Tenant.slug == "tenant-b",
            )
        ).scalar_one()
        assert tenant_b_membership.role == MembershipRole.OWNER
