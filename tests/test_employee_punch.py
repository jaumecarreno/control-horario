from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text

from app.extensions import db
from app.models import (
    Employee,
    EmployeeShiftAssignment,
    ExpectedHoursFrequency,
    LeavePolicyUnit,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
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


def _freeze_employee_now(monkeypatch, year: int, month: int, day: int):
    import app.blueprints.employee as employee_blueprint

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(year, month, day, 12, 0, 0)
            if tz is not None:
                return fixed.replace(tzinfo=tz)
            return fixed

    monkeypatch.setattr(employee_blueprint, "datetime", FrozenDateTime)


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


def test_open_shift_warning_is_visible_in_today_and_hours(client):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    create_entry = client.post("/me/punch/in", headers={"HX-Request": "true"})
    assert create_entry.status_code == 200

    today_page = client.get("/me/today")
    assert today_page.status_code == 200
    today_html = today_page.get_data(as_text=True)
    assert "Tienes una jornada abierta desde" in today_html

    hours_page = client.get("/me/hours")
    assert hours_page.status_code == 200
    hours_html = hours_page.get_data(as_text=True)
    assert "Aviso: tienes una jornada abierta desde" in hours_html


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


def test_punch_correction_request_creates_pending_request(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee_id = db.session.execute(select(Employee.id).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        source_event = TimeEvent(
            tenant_id=tenant_id,
            employee_id=employee_id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 12, 8, 30, tzinfo=timezone.utc),
        )
        db.session.add(source_event)
        db.session.commit()
        source_event_id = source_event.id

    create_request = client.post(
        "/me/punch-corrections",
        data={
            "source_event_id": str(source_event_id),
            "requested_date": "2026-02-12",
            "requested_hour": "9",
            "requested_minute": "0",
            "requested_kind": "IN",
            "reason": "Olvide fichar a la hora correcta al entrar.",
        },
        follow_redirects=True,
    )
    assert create_request.status_code == 200
    html = create_request.get_data(as_text=True)
    assert "Solicitud de rectificacion enviada." in html

    with app.app_context():
        request_row = db.session.execute(
            select(PunchCorrectionRequest).where(PunchCorrectionRequest.source_event_id == source_event_id)
        ).scalar_one()
        assert request_row.status == PunchCorrectionStatus.REQUESTED


def test_punch_correction_rejects_events_older_than_30_days(client, app, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 3, 15)

    with app.app_context():
        employee_id = db.session.execute(select(Employee.id).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        source_event = TimeEvent(
            tenant_id=tenant_id,
            employee_id=employee_id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc),
        )
        db.session.add(source_event)
        db.session.commit()
        source_event_id = source_event.id

    create_request = client.post(
        "/me/punch-corrections",
        data={
            "source_event_id": str(source_event_id),
            "requested_date": "2026-01-10",
            "requested_hour": "8",
            "requested_minute": "30",
            "requested_kind": "IN",
            "reason": "Necesito corregir este fichaje antiguo de entrada.",
        },
        follow_redirects=True,
    )
    assert create_request.status_code == 200
    html = create_request.get_data(as_text=True)
    assert "Solo se permiten rectificaciones de fichajes dentro de los ultimos 30 dias." in html

    with app.app_context():
        total = db.session.execute(select(func.count()).select_from(PunchCorrectionRequest)).scalar_one()
        assert total == 0


def test_punch_correction_rejects_duplicate_pending_for_same_event(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        source_event = TimeEvent(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type=TimeEventType.OUT,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 14, 18, 0, tzinfo=timezone.utc),
        )
        db.session.add(source_event)
        db.session.flush()
        db.session.add(
            PunchCorrectionRequest(
                tenant_id=employee.tenant_id,
                employee_id=employee.id,
                source_event_id=source_event.id,
                requested_ts=datetime(2026, 2, 14, 17, 30, tzinfo=timezone.utc),
                requested_type=TimeEventType.OUT,
                reason="Peticion previa pendiente para el mismo fichaje.",
                status=PunchCorrectionStatus.REQUESTED,
            )
        )
        db.session.commit()
        source_event_id = source_event.id

    duplicate = client.post(
        "/me/punch-corrections",
        data={
            "source_event_id": str(source_event_id),
            "requested_date": "2026-02-14",
            "requested_hour": "17",
            "requested_minute": "45",
            "requested_kind": "OUT",
            "reason": "Quiero enviar una segunda solicitud para el mismo evento.",
        },
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    html = duplicate.get_data(as_text=True)
    assert "Ya existe una solicitud pendiente para ese fichaje." in html

    with app.app_context():
        total = db.session.execute(
            select(func.count()).select_from(PunchCorrectionRequest).where(PunchCorrectionRequest.source_event_id == source_event_id)
        ).scalar_one()
        assert total == 1


def test_punch_correction_cancel_changes_status(client, app):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)

    with app.app_context():
        employee = db.session.execute(select(Employee).where(Employee.email == "employee@example.com")).scalar_one()
        source_event = TimeEvent(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            type=TimeEventType.IN,
            source=TimeEventSource.WEB,
            ts=datetime(2026, 2, 12, 8, 0, tzinfo=timezone.utc),
        )
        db.session.add(source_event)
        db.session.flush()
        correction = PunchCorrectionRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            source_event_id=source_event.id,
            requested_ts=datetime(2026, 2, 12, 8, 30, tzinfo=timezone.utc),
            requested_type=TimeEventType.IN,
            reason="Necesito corregir la hora de entrada.",
            status=PunchCorrectionStatus.REQUESTED,
        )
        db.session.add(correction)
        db.session.commit()
        correction_id = correction.id

    cancel = client.post(
        f"/me/punch-corrections/{correction_id}/cancel",
        data={"return_month": "2026-02"},
        follow_redirects=True,
    )
    assert cancel.status_code == 200
    html = cancel.get_data(as_text=True)
    assert "Solicitud de rectificacion cancelada." in html

    with app.app_context():
        refreshed = db.session.get(PunchCorrectionRequest, correction_id)
        assert refreshed is not None
        assert refreshed.status == PunchCorrectionStatus.CANCELLED
        assert refreshed.decided_at is not None




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


def test_presence_control_uses_employee_shift_history_for_expected_hours(client, app, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

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
    assert "Esperado 79:00" in html
    assert "Balance -79:00" in html


def test_presence_control_shows_current_and_future_days_without_balance_values(client, app, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

    with app.app_context():
        employee_id = db.session.execute(select(Employee.id).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        db.session.add(
            TimeEvent(
                tenant_id=tenant_id,
                employee_id=employee_id,
                type=TimeEventType.IN,
                source=TimeEventSource.WEB,
                ts=datetime(2026, 2, 17, 8, 30, tzinfo=timezone.utc),
            )
        )
        db.session.commit()

    page = client.get("/me/presence-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "16/02/2026" in html
    assert "17/02/2026" in html
    assert "18/02/2026" in html
    assert "Esperado 82:30" in html
    assert 'aria-label="Mes siguiente deshabilitado"' in html
    assert "/me/presence-control?month=2026-03" not in html
    assert re.search(
        r"<tr>\s*<td>17/02/2026</td>.*?<td>\s*Entrada 09:30\s*</td>\s*<td>-</td>\s*<td>-</td>\s*<td>-</td>\s*</tr>",
        html,
        re.DOTALL,
    )
    assert re.search(
        r"<tr>\s*<td>18/02/2026</td>\s*<td>\s*-\s*</td>\s*<td>-</td>\s*<td>-</td>\s*<td>-</td>\s*</tr>",
        html,
        re.DOTALL,
    )


def test_pause_control_shows_current_and_future_days_without_balance_values(client, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

    page = client.get("/me/pause-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "16/02/2026" in html
    assert "17/02/2026" in html
    assert "18/02/2026" in html
    assert "Esperado 05:30" in html
    assert 'aria-label="Mes siguiente deshabilitado"' in html
    assert "/me/pause-control?month=2026-03" not in html
    assert re.search(
        r"<tr>\s*<td>17/02/2026</td>\s*<td>\s*-\s*</td>\s*<td>-</td>\s*<td>-</td>\s*<td>-</td>\s*</tr>",
        html,
        re.DOTALL,
    )


def test_pause_control_current_day_ignores_in_out_markers(client, app, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

    with app.app_context():
        employee_id = db.session.execute(select(Employee.id).where(Employee.email == "employee@example.com")).scalar_one()
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        db.session.add_all(
            [
                TimeEvent(
                    tenant_id=tenant_id,
                    employee_id=employee_id,
                    type=TimeEventType.IN,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 17, 8, 26, tzinfo=timezone.utc),
                ),
                TimeEvent(
                    tenant_id=tenant_id,
                    employee_id=employee_id,
                    type=TimeEventType.OUT,
                    source=TimeEventSource.WEB,
                    ts=datetime(2026, 2, 17, 10, 50, tzinfo=timezone.utc),
                ),
            ]
        )
        db.session.commit()

    page = client.get("/me/pause-control?month=2026-02")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert re.search(
        r"<tr>\s*<td>17/02/2026</td>\s*<td>\s*-\s*</td>\s*<td>-</td>\s*<td>-</td>\s*<td>-</td>\s*</tr>",
        html,
        re.DOTALL,
    )
    assert "Entrada " not in html
    assert "Salida " not in html


def test_presence_control_redirects_future_month_to_current_month(client, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

    page = client.get("/me/presence-control?month=2026-03", follow_redirects=False)
    assert page.status_code == 302
    assert "/me/presence-control?month=2026-02" in page.headers["Location"]


def test_pause_control_redirects_future_month_to_current_month(client, monkeypatch):
    response = _login(client)
    assert response.status_code == 302
    _select_tenant_a(client)
    _freeze_employee_now(monkeypatch, 2026, 2, 17)

    page = client.get("/me/pause-control?month=2026-03", follow_redirects=False)
    assert page.status_code == 302
    assert "/me/pause-control?month=2026-02" in page.headers["Location"]


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

