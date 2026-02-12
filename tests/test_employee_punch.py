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


def test_manual_incident_creates_event_and_is_visible_in_presence(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    create_manual = client.post(
        "/me/incidents/manual",
        data={
            "manual_date": "2026-02-12",
            "manual_hour": "8",
            "manual_minute": "30",
            "manual_kind": "IN",
        },
        follow_redirects=False,
    )
    assert create_manual.status_code == 302

    events_count = _event_count()
    assert events_count == 1

    page = client.get("/me/presence-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Control de presencia" in html
    assert "Manual" in html
