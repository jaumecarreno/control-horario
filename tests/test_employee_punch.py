from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.extensions import db
from app.models import Employee, Tenant, TimeEvent, TimeEventSource, TimeEventType


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




def test_presence_control_renders_local_timezone_hour(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee_id = db.session.execute(select(Employee.id).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        db.session.add(
            TimeEvent(
                tenant_id=tenant_id,
                employee_id=employee_id,
                type=TimeEventType.IN,
                source=TimeEventSource.WEB,
                ts=datetime(2026, 2, 12, 8, 30, tzinfo=timezone.utc),
            )
        )
        db.session.commit()

    page = client.get("/me/presence-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "12/02/2026 09:30:00" in html


def test_pause_toggle_creates_break_events_and_updates_today_state(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    pause_start = client.post("/me/pause/toggle", headers={"HX-Request": "true"})
    assert pause_start.status_code == 200
    assert _event_count() == 1

    page_with_running_pause = pause_start.get_data(as_text=True)
    assert "Reanudar" in page_with_running_pause
    assert "Tiempo en Pausa:" in page_with_running_pause

    pause_end = client.post("/me/pause/toggle", headers={"HX-Request": "true"})
    assert pause_end.status_code == 200
    assert _event_count() == 2

    html_after_resume = pause_end.get_data(as_text=True)
    assert "Registrar PAUSA" in html_after_resume
    assert "Pausas hoy:" in html_after_resume

    event_types = list(db.session.execute(select(TimeEvent.type).order_by(TimeEvent.ts.asc())).scalars().all())
    assert event_types == [TimeEventType.BREAK_START, TimeEventType.BREAK_END]


def test_pause_control_page_shows_expected_columns(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    page = client.get("/me/pause-control")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Control de PAusas" in html
    assert "<th>Pausas</th>" in html
    assert "00:30" in html

    presence_page = client.get("/me/presence-control")
    assert presence_page.status_code == 200
    presence_html = presence_page.get_data(as_text=True)
    assert "Control de PAusas" in presence_html


def test_presence_control_falls_back_when_shifts_table_is_missing(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        db.session.execute(text("DROP TABLE shifts"))
        db.session.commit()

    page = client.get("/me/presence-control")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Control de presencia" in html
    assert "Sin turno configurado. Se aplica el valor por defecto." in html
