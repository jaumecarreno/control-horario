from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import Employee, Membership, MembershipRole, Tenant, User
from app.security import hash_secret


@pytest.fixture()
def role_access_context(app):
    with app.app_context():
        tenant = Tenant(id=uuid.uuid4(), name="Role Tenant", slug="role-tenant")
        db.session.add(tenant)
        db.session.flush()

        role_users: dict[MembershipRole, User] = {}
        for role in MembershipRole:
            user = User(
                id=uuid.uuid4(),
                email=f"role.{role.value.lower()}@example.com",
                password_hash=hash_secret("password123"),
                is_active=True,
            )
            db.session.add(user)
            role_users[role] = user
        db.session.flush()

        employee = Employee(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name="Employee User",
            email="role.employee@example.com",
            active=True,
        )
        db.session.add(employee)
        db.session.flush()

        for role, user in role_users.items():
            db.session.add(
                Membership(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    role=role,
                    employee_id=employee.id if role == MembershipRole.EMPLOYEE else None,
                )
            )

        db.session.commit()

        return {"tenant_id": str(tenant.id), "emails": {role: user.email for role, user in role_users.items()}}


def _login_for_role(client, ctx: dict, role: MembershipRole):
    login_response = client.post(
        "/login",
        data={"email": ctx["emails"][role], "password": "password123"},
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    with client.session_transaction() as session:
        session["active_tenant_id"] = ctx["tenant_id"]


def _expect_access_for_roles(client, ctx: dict, method: str, path: str, allowed_roles: set[MembershipRole], data: dict | None = None):
    for role in MembershipRole:
        client.post("/logout", follow_redirects=False)
        _login_for_role(client, ctx, role)
        response = client.open(path, method=method, data=data, follow_redirects=False)
        if role in allowed_roles:
            assert response.status_code != 403, f"{role.value} should access {path}"
        else:
            assert response.status_code == 403, f"{role.value} should be denied {path}"


def test_users_area_requires_manage_users_permission(client, role_access_context):
    _expect_access_for_roles(
        client,
        role_access_context,
        method="GET",
        path="/admin/users",
        allowed_roles={MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER},
    )


def test_approvals_area_requires_approve_leaves_permission(client, role_access_context):
    _expect_access_for_roles(
        client,
        role_access_context,
        method="GET",
        path="/admin/approvals",
        allowed_roles={MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER},
    )


def test_payroll_export_requires_export_permission(client, role_access_context):
    _expect_access_for_roles(
        client,
        role_access_context,
        method="GET",
        path="/admin/reports/payroll",
        allowed_roles={MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER},
    )


def test_manual_punch_requires_employee_profile(client, role_access_context):
    data = {
        "manual_date": "2026-02-10",
        "manual_hour": "10",
        "manual_minute": "15",
        "manual_kind": "IN",
    }
    _expect_access_for_roles(
        client,
        role_access_context,
        method="POST",
        path="/me/incidents/manual",
        data=data,
        allowed_roles={MembershipRole.EMPLOYEE},
    )

    with client.application.app_context():
        employee_user = db.session.execute(select(User).where(User.email == role_access_context["emails"][MembershipRole.EMPLOYEE])).scalar_one()
        membership = db.session.execute(select(Membership).where(Membership.user_id == employee_user.id)).scalar_one()
        assert membership.employee_id is not None
