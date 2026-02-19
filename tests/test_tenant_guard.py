from __future__ import annotations

import app.tenant as tenant_module


def _login(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )

def test_tenant_scoped_endpoint_requires_active_tenant(client):
    response = _login(client)
    assert response.status_code == 302
    assert "/select-tenant" in response.headers["Location"]

    me_today = client.get("/me/today", follow_redirects=False)
    assert me_today.status_code == 302
    assert "/select-tenant" in me_today.headers["Location"]

def test_admin_without_employee_profile_lands_on_admin_home(admin_only_client):
    response = admin_only_client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/admin/team-today" in response.headers["Location"]

    home = admin_only_client.get("/", follow_redirects=False)
    assert home.status_code == 302
    assert "/admin/team-today" in home.headers["Location"]


def test_select_tenant_page_handles_membership_lookup_errors(client, monkeypatch):
    def _raise_lookup_error():
        raise LookupError("invalid membership role value")

    monkeypatch.setattr(tenant_module, "current_membership", _raise_lookup_error)

    login_response = _login(client)
    assert login_response.status_code == 302
    assert "/select-tenant" in login_response.headers["Location"]

    page = client.get("/select-tenant", follow_redirects=False)
    assert page.status_code == 200
