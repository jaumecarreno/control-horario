"""Add employee shift assignment history.

Revision ID: 0003_add_employee_shift_assignments
Revises: 0002_add_shifts
Create Date: 2026-02-13
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_employee_shift_assignments"
down_revision: str | None = "0002_add_shifts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "employee_shift_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_employee_shift_assignment_dates"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "effective_from", name="uq_employee_shift_assignments_employee_from"),
    )
    op.create_index(
        "ix_employee_shift_assignments_tenant_employee_from",
        "employee_shift_assignments",
        ["tenant_id", "employee_id", "effective_from"],
        unique=False,
    )

    bind = op.get_bind()
    employees = sa.table(
        "employees",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
    )
    shifts = sa.table(
        "shifts",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    assignments = sa.table(
        "employee_shift_assignments",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("employee_id", sa.Uuid()),
        sa.column("shift_id", sa.Uuid()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    default_effective_from = date(1970, 1, 1)
    employee_rows = bind.execute(sa.select(employees.c.id, employees.c.tenant_id)).all()
    for employee_id, tenant_id in employee_rows:
        shift_id = bind.execute(
            sa.select(shifts.c.id)
            .where(shifts.c.tenant_id == tenant_id)
            .order_by(shifts.c.created_at.asc(), shifts.c.name.asc())
            .limit(1)
        ).scalar_one_or_none()
        if shift_id is None:
            continue
        bind.execute(
            sa.insert(assignments).values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                employee_id=employee_id,
                shift_id=shift_id,
                effective_from=default_effective_from,
                effective_to=None,
                created_at=datetime.now(timezone.utc),
            )
        )


def downgrade() -> None:
    op.drop_index("ix_employee_shift_assignments_tenant_employee_from", table_name="employee_shift_assignments")
    op.drop_table("employee_shift_assignments")
