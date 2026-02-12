from __future__ import annotations

from sqlalchemy import func, select

from app.extensions import db
from app.models import Tenant, TimeEvent


def _login(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _select_tenant_a(client):
    tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
    with client.session_transaction() as session:
        session["active_tenant_id"] = str(tenant_id)


def _event_count():
    return db.session.execute(select(func.count()).select_from(TimeEvent)).scalar_one()


def test_duplicate_punch_requires_confirmation(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    first = client.post("/me/punch/in", headers={"HX-Request": "true"})
    assert first.status_code == 200
    assert _event_count() == 1

    duplicate_without_confirmation = client.post("/me/punch/in", headers={"HX-Request": "true"})
    assert duplicate_without_confirmation.status_code == 200
    assert _event_count() == 1

    duplicate_with_confirmation = client.post(
        "/me/punch/in",
        data={"confirm_repeat": "1"},
        headers={"HX-Request": "true"},
    )
    assert duplicate_with_confirmation.status_code == 200
    assert _event_count() == 2
