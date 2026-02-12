from __future__ import annotations

from typing import Iterator
import uuid

import pytest
from sqlalchemy.pool import StaticPool

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Employee, Membership, MembershipRole, Tenant, User
from app.security import hash_secret


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite+pysqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }


@pytest.fixture()
def app() -> Iterator:
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        tenant_a = Tenant(id=uuid.uuid4(), name="Tenant A", slug="tenant-a")
        tenant_b = Tenant(id=uuid.uuid4(), name="Tenant B", slug="tenant-b")
        user = User(id=uuid.uuid4(), email="owner@example.com", password_hash=hash_secret("password123"), is_active=True)
        employee_a = Employee(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Owner Employee",
            email="employee@example.com",
            active=True,
        )

        db.session.add_all([tenant_a, tenant_b, user, employee_a])
        db.session.flush()
        db.session.add_all(
            [
                Membership(
                    tenant_id=tenant_a.id,
                    user_id=user.id,
                    role=MembershipRole.OWNER,
                    employee_id=employee_a.id,
                ),
                Membership(
                    tenant_id=tenant_b.id,
                    user_id=user.id,
                    role=MembershipRole.OWNER,
                    employee_id=None,
                ),
            ]
        )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_only_client(app):
    with app.app_context():
        tenant = Tenant(id=uuid.uuid4(), name="Admin Tenant", slug="admin-tenant")
        user = User(
            id=uuid.uuid4(),
            email="admin@example.com",
            password_hash=hash_secret("password123"),
            is_active=True,
        )
        db.session.add_all([tenant, user])
        db.session.flush()
        db.session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=user.id,
                role=MembershipRole.ADMIN,
                employee_id=None,
            )
        )
        db.session.commit()

    return app.test_client()
