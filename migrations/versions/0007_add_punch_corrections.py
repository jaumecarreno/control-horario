"""Add punch correction workflow tables and employee approver linkage.

Revision ID: 0007_add_punch_corrections
Revises: 0006_user_must_change_password
Create Date: 2026-02-19
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_add_punch_corrections"
down_revision: str | None = "0006_user_must_change_password"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


time_event_type = postgresql.ENUM(
    "IN",
    "OUT",
    "BREAK_START",
    "BREAK_END",
    name="time_event_type",
    create_type=False,
)
punch_correction_status = postgresql.ENUM(
    "REQUESTED",
    "APPROVED",
    "REJECTED",
    "CANCELLED",
    name="punch_correction_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    punch_correction_status.create(bind, checkfirst=True)

    op.add_column("employees", sa.Column("punch_approver_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_employees_punch_approver_user_id",
        "employees",
        "users",
        ["punch_approver_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "punch_correction_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("requested_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_type", time_event_type, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            punch_correction_status,
            nullable=False,
            server_default=sa.text("'REQUESTED'"),
        ),
        sa.Column("target_approver_user_id", sa.Uuid(), nullable=True),
        sa.Column("approver_user_id", sa.Uuid(), nullable=True),
        sa.Column("applied_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requested_type IN ('IN', 'OUT')",
            name="ck_punch_correction_requests_requested_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_event_id"], ["time_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_approver_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["applied_event_id"], ["time_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_punch_correction_requests_tenant_status",
        "punch_correction_requests",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_punch_correction_requests_employee_created",
        "punch_correction_requests",
        ["employee_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "time_event_supersessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("original_event_id", sa.Uuid(), nullable=False),
        sa.Column("replacement_event_id", sa.Uuid(), nullable=False),
        sa.Column("correction_request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["original_event_id"], ["time_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replacement_event_id"], ["time_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["correction_request_id"], ["punch_correction_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("original_event_id", name="uq_time_event_supersessions_original_event"),
        sa.UniqueConstraint("replacement_event_id", name="uq_time_event_supersessions_replacement_event"),
        sa.UniqueConstraint("correction_request_id", name="uq_time_event_supersessions_correction_request"),
    )
    op.create_index(
        "ix_time_event_supersessions_tenant_original",
        "time_event_supersessions",
        ["tenant_id", "original_event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_time_event_supersessions_tenant_original", table_name="time_event_supersessions")
    op.drop_table("time_event_supersessions")

    op.drop_index("ix_punch_correction_requests_employee_created", table_name="punch_correction_requests")
    op.drop_index("ix_punch_correction_requests_tenant_status", table_name="punch_correction_requests")
    op.drop_table("punch_correction_requests")

    op.drop_constraint("fk_employees_punch_approver_user_id", "employees", type_="foreignkey")
    op.drop_column("employees", "punch_approver_user_id")

    bind = op.get_bind()
    punch_correction_status.drop(bind, checkfirst=True)
