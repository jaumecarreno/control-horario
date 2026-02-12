from __future__ import annotations


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


