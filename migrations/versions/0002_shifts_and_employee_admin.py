"""Add shifts and employee shift assignment.

Revision ID: 0002_shifts_and_employee_admin
Revises: 0001_initial
Create Date: 2026-02-12
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_shifts_and_employee_admin"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


shift_period = sa.Enum("ANNUAL", "MONTHLY", "WEEKLY", "DAILY", name="shift_period")


def upgrade() -> None:
    bind = op.get_bind()
    shift_period.create(bind, checkfirst=True)

    op.create_table(
        "shifts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("break_counts_as_work", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expected_hours", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expected_hours_period", shift_period, nullable=False, server_default=sa.text("'DAILY'")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shifts_tenant_name", "shifts", ["tenant_id", "name"], unique=False)

    op.add_column("employees", sa.Column("shift_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_employees_shift_id", "employees", "shifts", ["shift_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_employees_shift_id", "employees", type_="foreignkey")
    op.drop_column("employees", "shift_id")

    op.drop_index("ix_shifts_tenant_name", table_name="shifts")
    op.drop_table("shifts")

    bind = op.get_bind()
    shift_period.drop(bind, checkfirst=True)
