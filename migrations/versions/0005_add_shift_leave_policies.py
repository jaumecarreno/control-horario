"""Add shift leave policies and optional leave request policy linkage.

Revision ID: 0005_shift_leave_policies
Revises: 0004_emp_active_status
Create Date: 2026-02-16
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_shift_leave_policies"
down_revision: str | None = "0004_emp_active_status"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


leave_policy_unit = sa.Enum("DAYS", "HOURS", name="leave_policy_unit")


def upgrade() -> None:
    bind = op.get_bind()
    tenant_id = bind.execute(sa.text("SELECT id::text FROM tenants ORDER BY id LIMIT 1")).scalar_one_or_none()
    if tenant_id is None:
        tenant_id = "00000000-0000-0000-0000-000000000000"
    bind.exec_driver_sql(f"SET LOCAL app.tenant_id = '{tenant_id}'")

    op.create_table(
        "shift_leave_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("unit", leave_policy_unit, nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_shift_leave_policies_amount_positive"),
        sa.CheckConstraint("valid_to >= valid_from", name="ck_shift_leave_policies_dates"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shift_leave_policies_tenant_shift",
        "shift_leave_policies",
        ["tenant_id", "shift_id"],
        unique=False,
    )

    op.add_column("leave_requests", sa.Column("leave_policy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_leave_requests_leave_policy_id",
        "leave_requests",
        "shift_leave_policies",
        ["leave_policy_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_leave_requests_leave_policy_id", "leave_requests", type_="foreignkey")
    op.drop_column("leave_requests", "leave_policy_id")

    op.drop_index("ix_shift_leave_policies_tenant_shift", table_name="shift_leave_policies")
    op.drop_table("shift_leave_policies")

    bind = op.get_bind()
    leave_policy_unit.drop(bind, checkfirst=True)
