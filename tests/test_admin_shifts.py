from __future__ import annotations

from sqlalchemy import select, text

from app.extensions import db
from app.models import Shift


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def test_admin_can_create_shift(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302
    assert "/admin/team-today" in login_response.headers["Location"]

    create_response = admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "45",
            "expected_hours": "8",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302
    assert "/admin/turnos" in create_response.headers["Location"]

    shifts_page = admin_only_client.get("/admin/turnos", follow_redirects=False)
    body = shifts_page.get_data(as_text=True)
    assert shifts_page.status_code == 200
    assert "General" in body
    assert "45" in body
    assert "Diarias" in body


def test_admin_shift_name_must_be_unique(admin_only_client):
    _login_admin(admin_only_client)
    payload = {
        "name": "Media jornada",
        "break_minutes": "30",
        "expected_hours": "4",
        "expected_hours_frequency": "DAILY",
    }

    first_response = admin_only_client.post("/admin/turnos/new", data=payload, follow_redirects=False)
    assert first_response.status_code == 302

    second_response = admin_only_client.post("/admin/turnos/new", data=payload, follow_redirects=True)
    body = second_response.get_data(as_text=True)
    assert second_response.status_code == 200
    assert "Ya existe un turno con ese nombre." in body


def test_admin_turnos_page_handles_missing_shifts_table(admin_only_client):
    _login_admin(admin_only_client)

    with admin_only_client.application.app_context():
        db.session.execute(text("DROP TABLE shifts"))
        db.session.commit()

    shifts_page = admin_only_client.get("/admin/turnos", follow_redirects=False)
    body = shifts_page.get_data(as_text=True)

    assert shifts_page.status_code == 200
    assert "No se pudieron cargar los turnos." in body
    assert "No hay turnos creados." in body


def test_admin_can_edit_shift(admin_only_client):
    _login_admin(admin_only_client)
    create_response = admin_only_client.post(
        "/admin/turnos/new",
        data={
            "name": "General",
            "break_counts_as_worked_bool": "y",
            "break_minutes": "30",
            "expected_hours": "8",
            "expected_hours_frequency": "DAILY",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    with admin_only_client.application.app_context():
        shift = db.session.execute(select(Shift).where(Shift.name == "General")).scalar_one()
        shift_id = shift.id

    update_response = admin_only_client.post(
        f"/admin/turnos/{shift_id}/edit",
        data={
            "name": "General revisado",
            "break_minutes": "20",
            "expected_hours": "35",
            "expected_hours_frequency": "WEEKLY",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 302
    assert "/admin/turnos" in update_response.headers["Location"]

    shifts_page = admin_only_client.get("/admin/turnos", follow_redirects=False)
    body = shifts_page.get_data(as_text=True)
    assert shifts_page.status_code == 200
    assert "General revisado" in body
    assert "20" in body
    assert "Semanales" in body
