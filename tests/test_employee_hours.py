from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.extensions import db
from app.models import Employee, Tenant, TimeEvent, TimeEventSource, TimeEventType


def _login_owner(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _select_tenant(client, slug: str):
    tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one()
    with client.session_transaction() as session:
        session["active_tenant_id"] = str(tenant_id)


def test_me_hours_data_returns_period_totals_and_daily_details(client, app):
    login_response = _login_owner(client)
    assert login_response.status_code == 302
    _select_tenant(client, "tenant-a")

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        db.session.add_all(
            [
                TimeEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    type=TimeEventType.IN,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    type=TimeEventType.BREAK_START,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    type=TimeEventType.BREAK_END,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 10, 12, 30, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    type=TimeEventType.OUT,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 10, 17, 0, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    type=TimeEventType.IN,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 11, 9, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.session.commit()

    response = client.get(
        "/me/hours/data?preset=custom&anchor=2026-02-10&date_from=2026-02-10&date_to=2026-02-11",
        follow_redirects=False,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None

    totals = payload["totals"]
    assert totals["worked_minutes"] == 540
    assert totals["paused_minutes"] == 30
    assert totals["net_minutes"] == 510
    assert totals["worked_display"] == "09:00"
    assert totals["paused_display"] == "00:30"
    assert totals["net_display"] == "08:30"

    rows_by_date = {row["date"]: row for row in payload["days"]}
    assert set(rows_by_date) == {"2026-02-10", "2026-02-11"}

    first_day = rows_by_date["2026-02-10"]
    assert first_day["worked_minutes"] == 540
    assert first_day["paused_minutes"] == 30
    assert first_day["net_minutes"] == 510
    assert first_day["is_open"] is False
    assert len(first_day["in_out"]) == 2
    assert len(first_day["pauses"]) == 1

    second_day = rows_by_date["2026-02-11"]
    assert second_day["worked_minutes"] == 0
    assert second_day["paused_minutes"] == 0
    assert second_day["net_minutes"] == 0
    assert second_day["is_open"] is True
