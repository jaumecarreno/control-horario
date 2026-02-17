from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text

from app.extensions import db
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Shift,
    ShiftLeavePolicy,
    Tenant,
    TimeEvent,
    TimeEventSource,
    TimeEventType,
)


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
    assert "Historial" in html
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
    assert "Finalizar pausa" in page_with_running_pause
    assert "Tiempo en Pausa:" in page_with_running_pause

    pause_end = client.post("/me/pause/toggle", headers={"HX-Request": "true"})
    assert pause_end.status_code == 200
    assert _event_count() == 2

    html_after_resume = pause_end.get_data(as_text=True)
    assert "Iniciar pausa" in html_after_resume
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
    assert "Control de pausas" in html
    assert "<th>Pausas</th>" in html
    assert "00:30" in html

    presence_page = client.get("/me/presence-control")
    assert presence_page.status_code == 200
    presence_html = presence_page.get_data(as_text=True)
    assert "Control de pausas" in presence_html


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
    assert "Historial" in html
    assert "Sin turno configurado. Se aplica el valor por defecto." in html


def test_presence_control_uses_employee_shift_history_for_expected_hours(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()

        full_time = Shift(
            tenant_id=tenant_id,
            name="General",
            break_counts_as_worked_bool=True,
            break_minutes=30,
            expected_hours=Decimal("7.50"),
            expected_hours_frequency=ExpectedHoursFrequency.DAILY,
        )
        part_time = Shift(
            tenant_id=tenant_id,
            name="Parcial",
            break_counts_as_worked_bool=True,
            break_minutes=30,
            expected_hours=Decimal("4.00"),
            expected_hours_frequency=ExpectedHoursFrequency.DAILY,
        )
        db.session.add_all([full_time, part_time])
        db.session.flush()
        db.session.add_all(
            [
                EmployeeShiftAssignment(
                    tenant_id=tenant_id,
                    employee_id=employee.id,
                    shift_id=full_time.id,
                    effective_from=date(2026, 2, 1),
                    effective_to=date(2026, 2, 15),
                ),
                EmployeeShiftAssignment(
                    tenant_id=tenant_id,
                    employee_id=employee.id,
                    shift_id=part_time.id,
                    effective_from=date(2026, 2, 16),
                    effective_to=None,
                ),
            ]
        )
        db.session.commit()

    page = client.get("/me/presence-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Esperado 115:00" in html
    assert "Balance -115:00" in html


def test_me_today_handles_missing_shift_leave_policies(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    page = client.get("/me/today")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Vacaciones permisos" in html
    assert "sin bolsas activas" in html


def test_me_today_shows_shift_leave_policy_balance(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()

        shift = Shift(
            tenant_id=tenant_id,
            name="General",
            break_counts_as_worked_bool=True,
            break_minutes=30,
            expected_hours=Decimal("7.50"),
            expected_hours_frequency=ExpectedHoursFrequency.DAILY,
        )
        leave_type = LeaveType(
            tenant_id=tenant_id,
            code="VACACIONES",
            name="Vacaciones",
            paid_bool=False,
            requires_approval_bool=True,
            counts_as_worked_bool=False,
        )
        db.session.add_all([shift, leave_type])
        db.session.flush()
        db.session.add(
            EmployeeShiftAssignment(
                tenant_id=tenant_id,
                employee_id=employee.id,
                shift_id=shift.id,
                effective_from=date(2020, 1, 1),
                effective_to=None,
            )
        )
        policy = ShiftLeavePolicy(
            tenant_id=tenant_id,
            shift_id=shift.id,
            leave_type_id=leave_type.id,
            name="Vacaciones",
            amount=Decimal("22"),
            unit=LeavePolicyUnit.DAYS,
            valid_from=date(2020, 1, 1),
            valid_to=date(2030, 12, 31),
        )
        db.session.add(policy)
        db.session.flush()
        db.session.add(
            LeaveRequest(
                tenant_id=tenant_id,
                employee_id=employee.id,
                type_id=leave_type.id,
                leave_policy_id=policy.id,
                date_from=date(2026, 2, 1),
                date_to=date(2026, 2, 5),
                minutes=None,
                status=LeaveRequestStatus.APPROVED,
            )
        )
        db.session.commit()

    page = client.get("/me/today")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Vacaciones" in html
    assert "22 totales" in html
    assert "Usado 5" in html
    assert "17" in html


def test_me_leaves_does_not_crash_without_shift_leave_policies(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    page = client.get("/me/leaves")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "No hay vacaciones o permisos configurados para tu turno actual." in html

    submit = client.post("/me/leaves", data={}, follow_redirects=True)
    assert submit.status_code == 200
    submit_html = submit.get_data(as_text=True)
    assert "No hay vacaciones o permisos definidos para tu turno actual." in submit_html

