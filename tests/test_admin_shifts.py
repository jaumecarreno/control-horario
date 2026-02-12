from __future__ import annotations


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
