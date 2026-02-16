"""Database models."""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Any

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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


class LeavePolicyUnit(str, enum.Enum):
    DAYS = "DAYS"
    HOURS = "HOURS"


class ExpectedHoursFrequency(str, enum.Enum):
    YEARLY = "YEARLY"
    MONTHLY = "MONTHLY"
    WEEKLY = "WEEKLY"
    DAILY = "DAILY"


# Backward-compatible alias used in tests/imports.
ShiftPeriod = ExpectedHoursFrequency


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
    active_status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class Shift(db.Model):
    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_tenant_name", "tenant_id", "name"),
        UniqueConstraint("tenant_id", "name", name="uq_shifts_tenant_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    break_counts_as_worked_bool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    expected_hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=Decimal("8.00"))
    expected_hours_frequency: Mapped[ExpectedHoursFrequency] = mapped_column(
        Enum(ExpectedHoursFrequency, name="expected_hours_frequency"),
        nullable=False,
        default=ExpectedHoursFrequency.DAILY,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class EmployeeShiftAssignment(db.Model):
    __tablename__ = "employee_shift_assignments"
    __table_args__ = (
        Index("ix_employee_shift_assignments_tenant_employee_from", "tenant_id", "employee_id", "effective_from"),
        UniqueConstraint("employee_id", "effective_from", name="uq_employee_shift_assignments_employee_from"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_employee_shift_assignment_dates"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


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


class ShiftLeavePolicy(db.Model):
    __tablename__ = "shift_leave_policies"
    __table_args__ = (
        Index("ix_shift_leave_policies_tenant_shift", "tenant_id", "shift_id"),
        CheckConstraint("amount > 0", name="ck_shift_leave_policies_amount_positive"),
        CheckConstraint("valid_to >= valid_from", name="ck_shift_leave_policies_dates"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    unit: Mapped[LeavePolicyUnit] = mapped_column(Enum(LeavePolicyUnit, name="leave_policy_unit"), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"
    __table_args__ = (Index("ix_leave_requests_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    type_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False)
    leave_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shift_leave_policies.id", ondelete="SET NULL"), nullable=True
    )
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
