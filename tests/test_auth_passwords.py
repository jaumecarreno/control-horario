from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models import User
from app.security import verify_secret


def _login_owner(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def test_me_security_password_rejects_wrong_current_password(client):
    login_response = _login_owner(client)
    assert login_response.status_code == 302

    response = client.post(
        "/me/security/password",
        data={
            "current_password": "incorrecta123",
            "new_password": "nueva-password-123",
            "confirm_password": "nueva-password-123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 400
    assert "contraseña actual es incorrecta" in response.get_data(as_text=True)


def test_me_security_password_updates_password_successfully(client):
    login_response = _login_owner(client)
    assert login_response.status_code == 302

    response = client.post(
        "/me/security/password",
        data={
            "current_password": "password123",
            "new_password": "updated-password-123",
            "confirm_password": "updated-password-123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with client.application.app_context():
        user = db.session.execute(select(User).where(User.email == "owner@example.com")).scalar_one()
        assert verify_secret(user.password_hash, "updated-password-123") is True
        assert user.must_change_password is False


def test_login_redirects_to_password_change_when_enforced(client):
    with client.application.app_context():
        user = db.session.execute(select(User).where(User.email == "owner@example.com")).scalar_one()
        user.must_change_password = True
        db.session.commit()

    login_response = _login_owner(client)
    assert login_response.status_code == 302
    assert login_response.headers["Location"].endswith("/me/security/password")
