"""Track when employee active status changes.

Revision ID: 0004_emp_active_status
Revises: 0003_emp_shift_assign
Create Date: 2026-02-13
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_emp_active_status"
down_revision: str | None = "0003_emp_shift_assign"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "active_status_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("employees", "active_status_changed_at")
