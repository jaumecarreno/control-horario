"""Add shifts table.

Revision ID: 0002_add_shifts
Revises: 0001_initial
Create Date: 2026-02-12
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_shifts"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


expected_hours_frequency = sa.Enum("YEARLY", "MONTHLY", "WEEKLY", "DAILY", name="expected_hours_frequency")


def upgrade() -> None:

    op.create_table(
        "shifts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("break_counts_as_worked_bool", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("expected_hours", sa.Numeric(precision=8, scale=2), nullable=False, server_default=sa.text("8.00")),
        sa.Column("expected_hours_frequency", expected_hours_frequency, nullable=False, server_default=sa.text("'DAILY'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_shifts_tenant_name"),
    )
    op.create_index("ix_shifts_tenant_name", "shifts", ["tenant_id", "name"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_shifts_tenant_name", table_name="shifts")
    op.drop_table("shifts")

    bind = op.get_bind()
    expected_hours_frequency.drop(bind, checkfirst=True)

