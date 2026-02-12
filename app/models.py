"""Database models."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class MembershipRole(str, enum.Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    EMPLOYEE = "EMPLOYEE"
    AGENCY = "AGENCY"


class TimeEventType(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"
    BREAK_START = "BREAK_START"
    BREAK_END = "BREAK_END"


class TimeEventSource(str, enum.Enum):
    WEB = "WEB"
    MOBILE = "MOBILE"
    KIOSK = "KIOSK"
    API = "API"


class LeaveRequestStatus(str, enum.Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class Tenant(db.Model):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    payroll_cutoff_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="tenant")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")

    def get_id(self) -> str:
        return str(self.id)


class Membership(db.Model):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
        Index("ix_memberships_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[MembershipRole] = mapped_column(Enum(MembershipRole, name="membership_role"), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Site(db.Model):
    __tablename__ = "sites"
    __table_args__ = (Index("ix_sites_tenant_name", "tenant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class ShiftPeriod(str, enum.Enum):
    ANNUAL = "ANNUAL"
    MONTHLY = "MONTHLY"
    WEEKLY = "WEEKLY"
    DAILY = "DAILY"


class Shift(db.Model):
    __tablename__ = "shifts"
    __table_args__ = (Index("ix_shifts_tenant_name", "tenant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    break_counts_as_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_hours_period: Mapped[ShiftPeriod] = mapped_column(Enum(ShiftPeriod, name="shift_period"), nullable=False)


class Employee(db.Model):
    __tablename__ = "employees"
    __table_args__ = (
        Index("ix_employees_tenant_name", "tenant_id", "name"),
        UniqueConstraint("tenant_id", "email", name="uq_employees_tenant_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True)

    shift: Mapped[Shift | None] = relationship()


class TimeEvent(db.Model):
    __tablename__ = "time_events"
    __table_args__ = (Index("ix_time_events_tenant_employee_ts", "tenant_id", "employee_id", "ts"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    type: Mapped[TimeEventType] = mapped_column(Enum(TimeEventType, name="time_event_type"), nullable=False)
    source: Mapped[TimeEventSource] = mapped_column(
        Enum(TimeEventSource, name="time_event_source"),
        nullable=False,
        default=TimeEventSource.WEB,
    )
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


@event.listens_for(TimeEvent, "before_update")
def prevent_time_event_update(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("time_events are append-only")


@event.listens_for(TimeEvent, "before_delete")
def prevent_time_event_delete(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("time_events are append-only")


class TimeAdjustment(db.Model):
    __tablename__ = "time_adjustments"
    __table_args__ = (Index("ix_time_adjustments_tenant_employee", "tenant_id", "employee_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("time_events.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    minutes_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class LeaveType(db.Model):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_leave_types_tenant_code"),
        Index("ix_leave_types_tenant_code", "tenant_id", "code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    paid_bool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_approval_bool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    counts_as_worked_bool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"
    __table_args__ = (Index("ix_leave_requests_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[LeaveRequestStatus] = mapped_column(
        Enum(LeaveRequestStatus, name="leave_request_status"),
        nullable=False,
        default=LeaveRequestStatus.REQUESTED,
    )
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_ts", "tenant_id", "ts"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
