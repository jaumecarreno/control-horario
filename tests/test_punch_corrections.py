from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Employee,
    Membership,
    MembershipRole,
    PunchCorrectionRequest,
    PunchCorrectionStatus,
    Tenant,
    TimeEvent,
    TimeEventSource,
    TimeEventSupersession,
    TimeEventType,
    User,
)
from app.security import hash_secret


def _login_owner(client):
    return client.post(
        "/login",
        data={"email": "owner@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _login_admin(client):
    return client.post(
        "/login",
        data={"email": "admin@example.com", "password": "password123"},
        follow_redirects=False,
    )


def _create_admin_pending_correction_request(
    *,
    tenant_slug: str = "admin-tenant",
    target_approver_user_id: uuid.UUID | None = None,
    with_attachment: bool = False,
) -> PunchCorrectionRequest:
    tenant = db.session.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one()
    employee = Employee(
        tenant_id=tenant.id,
        name="Empleado rectificacion",
        email=f"rectificacion-{uuid.uuid4()}@example.com",
        active=True,
    )
    db.session.add(employee)
    db.session.flush()
    source_event = TimeEvent(
        tenant_id=tenant.id,
        employee_id=employee.id,
        type=TimeEventType.IN,
        source=TimeEventSource.WEB,
        ts=datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
    )
    db.session.add(source_event)
    db.session.flush()
    correction = PunchCorrectionRequest(
        tenant_id=tenant.id,
        employee_id=employee.id,
        source_event_id=source_event.id,
        requested_ts=datetime(2026, 2, 10, 8, 30, tzinfo=timezone.utc),
        requested_type=TimeEventType.IN,
        reason="Ajuste por error al fichar la hora de entrada.",
        attachment_name="adjunto.pdf" if with_attachment else None,
        attachment_mime="application/pdf" if with_attachment else None,
        attachment_blob=b"%PDF-1.4 evidencia" if with_attachment else None,
        status=PunchCorrectionStatus.REQUESTED,
        target_approver_user_id=target_approver_user_id,
    )
    db.session.add(correction)
    db.session.commit()
    return correction


def test_admin_punch_correction_approve_creates_replacement_and_supersession(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        correction = _create_admin_pending_correction_request()
        correction_id = correction.id

    approve = admin_only_client.post(
        f"/admin/punch-corrections/{correction_id}/approve",
        data={"comment": "Aprobada con ajuste validado."},
        follow_redirects=True,
    )
    assert approve.status_code == 200
    html = approve.get_data(as_text=True)
    assert "Solicitud de rectificacion aprobada." in html

    with admin_only_client.application.app_context():
        refreshed = db.session.get(PunchCorrectionRequest, correction_id)
        assert refreshed is not None
        assert refreshed.status == PunchCorrectionStatus.APPROVED
        assert refreshed.approver_user_id is not None
        assert refreshed.applied_event_id is not None
        assert refreshed.approver_comment == "Aprobada con ajuste validado."

        replacement_event = db.session.get(TimeEvent, refreshed.applied_event_id)
        assert replacement_event is not None
        assert replacement_event.type == TimeEventType.IN
        assert replacement_event.ts.replace(tzinfo=None) == datetime(2026, 2, 10, 8, 30)

        supersession = db.session.execute(
            select(TimeEventSupersession).where(TimeEventSupersession.correction_request_id == correction_id)
        ).scalar_one()
        assert supersession.original_event_id == refreshed.source_event_id
        assert supersession.replacement_event_id == replacement_event.id


def test_admin_can_download_correction_attachment(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        correction = _create_admin_pending_correction_request(with_attachment=True)
        correction_id = correction.id

    response = admin_only_client.get(
        f"/admin/punch-corrections/{correction_id}/attachment",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert response.data.startswith(b"%PDF-1.4")


def test_admin_punch_correction_reject_keeps_original_event(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        correction = _create_admin_pending_correction_request()
        correction_id = correction.id
        source_event_id = correction.source_event_id

    reject = admin_only_client.post(
        f"/admin/punch-corrections/{correction_id}/reject",
        follow_redirects=True,
    )
    assert reject.status_code == 200
    html = reject.get_data(as_text=True)
    assert "Solicitud de rectificacion rechazada." in html

    with admin_only_client.application.app_context():
        refreshed = db.session.get(PunchCorrectionRequest, correction_id)
        assert refreshed is not None
        assert refreshed.status == PunchCorrectionStatus.REJECTED
        assert refreshed.applied_event_id is None

        supersession_count = db.session.execute(
            select(func.count()).select_from(TimeEventSupersession).where(TimeEventSupersession.original_event_id == source_event_id)
        ).scalar_one()
        assert supersession_count == 0

        event_count = db.session.execute(select(func.count()).select_from(TimeEvent)).scalar_one()
        assert event_count >= 1


def test_admin_punch_correction_redecide_returns_conflict(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        correction = _create_admin_pending_correction_request()
        correction_id = correction.id

    first = admin_only_client.post(
        f"/admin/punch-corrections/{correction_id}/approve",
        follow_redirects=False,
    )
    assert first.status_code == 302

    second = admin_only_client.post(
        f"/admin/punch-corrections/{correction_id}/reject",
        follow_redirects=False,
    )
    assert second.status_code == 409
    second_html = second.get_data(as_text=True)
    assert "La solicitud ya fue decidida." in second_html


def test_admin_punch_correction_forbidden_when_target_approver_is_other_user(admin_only_client):
    login_response = _login_admin(admin_only_client)
    assert login_response.status_code == 302

    with admin_only_client.application.app_context():
        tenant = db.session.execute(select(Tenant).where(Tenant.slug == "admin-tenant")).scalar_one()
        extra_admin = User(
            id=uuid.uuid4(),
            email=f"target-approver-{uuid.uuid4()}@example.com",
            password_hash=hash_secret("password123"),
            is_active=True,
        )
        db.session.add(extra_admin)
        db.session.flush()
        db.session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=extra_admin.id,
                role=MembershipRole.ADMIN,
                employee_id=None,
            )
        )
        db.session.commit()

        correction = _create_admin_pending_correction_request(target_approver_user_id=extra_admin.id)
        correction_id = correction.id

    response = admin_only_client.post(
        f"/admin/punch-corrections/{correction_id}/approve",
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_presence_control_uses_replacement_and_hides_superseded_original(client, app):
    login_response = _login_owner(client)
    assert login_response.status_code == 302

    with app.app_context():
        tenant_id = db.session.execute(select(Tenant.id).where(Tenant.slug == "tenant-a")).scalar_one()
        with client.session_transaction() as session:
            session["active_tenant_id"] = str(tenant_id)

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
            reason="Rectificacion aprobada para presencia.",
            status=PunchCorrectionStatus.APPROVED,
            approver_user_id=None,
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

    page = client.get("/me/presence-control?month=2026-02", follow_redirects=True)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "12/02/2026 09:30:00" in html
    assert "12/02/2026 09:00:00" not in html
