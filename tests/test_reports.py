from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select

from app.extensions import db
from app.models import (
    Employee,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
    Tenant,
    TimeEvent,
    TimeEventSource,
    TimeEventSupersession,
    TimeEventType,
)


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


def test_control_report_supports_csv_json_xlsx_and_pdf(client, app):
    login_response = _login_owner(client)
    assert login_response.status_code == 302
    _activate_tenant(client, "tenant-a")

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        event = TimeEvent(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
        )
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    common_data = {
        "report_type": "control",
        "employee_id": "",
        "date_from": "2026-02-10",
        "date_to": "2026-02-10",
    }

    csv_response = client.post(
        "/admin/reports/payroll/export",
        data={**common_data, "output_format": "csv"},
        follow_redirects=False,
    )
    assert csv_response.status_code == 200
    assert ".csv" in csv_response.headers["Content-Disposition"]
    csv_content = csv_response.get_data(as_text=True)
    assert "employee_id,employee_name,event_id,timestamp_utc,timestamp_local,event_type,source,manual" in csv_content
    assert str(event_id) in csv_content

    json_response = client.post(
        "/admin/reports/payroll/export",
        data={**common_data, "output_format": "json"},
        follow_redirects=False,
    )
    assert json_response.status_code == 200
    assert ".json" in json_response.headers["Content-Disposition"]
    payload = json.loads(json_response.get_data(as_text=True))
    assert payload["report_type"] == "control"
    assert payload["row_count"] >= 1
    assert any(row["event_id"] == str(event_id) for row in payload["rows"])

    xlsx_response = client.post(
        "/admin/reports/payroll/export",
        data={**common_data, "output_format": "xlsx"},
        follow_redirects=False,
    )
    assert xlsx_response.status_code == 200
    assert ".xlsx" in xlsx_response.headers["Content-Disposition"]
    assert xlsx_response.data.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(xlsx_response.data), "r") as workbook:
        assert "xl/workbook.xml" in workbook.namelist()

    pdf_response = client.post(
        "/admin/reports/payroll/export",
        data={**common_data, "output_format": "pdf"},
        follow_redirects=False,
    )
    assert pdf_response.status_code == 200
    assert ".pdf" in pdf_response.headers["Content-Disposition"]
    assert pdf_response.data.startswith(b"%PDF-")


def test_executive_report_supports_tenant_scope_and_employee_filter(client, app):
    login_response = _login_owner(client)
    assert login_response.status_code == 302
    _activate_tenant(client, "tenant-a")

    with app.app_context():
        tenant = db.session.execute(select(Tenant).where(Tenant.slug == "tenant-a")).scalar_one()
        employee_with_events = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        employee_without_events = Employee(
            tenant_id=tenant.id,
            name="Sin fichajes",
            email="sin.fichajes@example.com",
            active=True,
        )
        db.session.add(employee_without_events)
        db.session.flush()
        db.session.add_all(
            [
                TimeEvent(
                    tenant_id=tenant.id,
                    employee_id=employee_with_events.id,
                    type=TimeEventType.IN,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 11, 8, 0, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=tenant.id,
                    employee_id=employee_with_events.id,
                    type=TimeEventType.OUT,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 11, 16, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.session.commit()
        employee_without_events_id = employee_without_events.id
        employee_with_events_id = employee_with_events.id

    full_response = client.post(
        "/admin/reports/payroll/export",
        data={
            "report_type": "executive",
            "output_format": "json",
            "employee_id": "",
            "date_from": "2026-02-11",
            "date_to": "2026-02-11",
        },
        follow_redirects=False,
    )
    assert full_response.status_code == 200
    payload = json.loads(full_response.get_data(as_text=True))
    rows_by_employee_id = {row["employee_id"]: row for row in payload["rows"]}
    assert str(employee_with_events_id) in rows_by_employee_id
    assert str(employee_without_events_id) in rows_by_employee_id
    assert rows_by_employee_id[str(employee_with_events_id)]["total_events"] == 2
    assert rows_by_employee_id[str(employee_with_events_id)]["worked_minutes"] == 480
    assert rows_by_employee_id[str(employee_without_events_id)]["total_events"] == 0

    filtered_response = client.post(
        "/admin/reports/payroll/export",
        data={
            "report_type": "executive",
            "output_format": "json",
            "employee_id": str(employee_without_events_id),
            "date_from": "2026-02-11",
            "date_to": "2026-02-11",
        },
        follow_redirects=False,
    )
    assert filtered_response.status_code == 200
    filtered_payload = json.loads(filtered_response.get_data(as_text=True))
    assert filtered_payload["row_count"] == 1
    assert filtered_payload["rows"][0]["employee_id"] == str(employee_without_events_id)
    assert filtered_payload["rows"][0]["total_events"] == 0


def test_control_report_hides_superseded_original_events(client, app):
    login_response = _login_owner(client)
    assert login_response.status_code == 302
    _activate_tenant(client, "tenant-a")

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        original_event = TimeEvent(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 12, 8, 0, tzinfo=timezone.utc),
        )
        db.session.add(original_event)
        db.session.flush()

        replacement_event = TimeEvent(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 12, 8, 30, tzinfo=timezone.utc),
        )
        db.session.add(replacement_event)
        db.session.flush()

        correction = PunchCorrectionRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            source_event_id=original_event.id,
            requested_ts=replacement_event.ts,
            requested_type=TimeEventType.IN,
            reason="Rectificacion aprobada para reporte.",
            status=PunchCorrectionStatus.APPROVED,
            applied_event_id=replacement_event.id,
        )
        db.session.add(correction)
        db.session.flush()

        db.session.add(
            TimeEventSupersession(
                tenant_id=employee.tenant_id,
                original_event_id=original_event.id,
                replacement_event_id=replacement_event.id,
                correction_request_id=correction.id,
            )
        )
        db.session.commit()
        original_event_id = original_event.id
        replacement_event_id = replacement_event.id

    response = client.post(
        "/admin/reports/payroll/export",
        data={
            "report_type": "control",
            "output_format": "json",
            "employee_id": "",
            "date_from": "2026-02-12",
            "date_to": "2026-02-12",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    event_ids = {row["event_id"] for row in payload["rows"]}
    assert str(original_event_id) not in event_ids
    assert str(replacement_event_id) in event_ids
