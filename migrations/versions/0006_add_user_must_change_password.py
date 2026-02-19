"""Add must_change_password flag to users.

Revision ID: 0006_user_must_change_password
Revises: 0005_shift_leave_policies
Create Date: 2026-02-19
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_user_must_change_password"
down_revision: str | None = "0005_shift_leave_policies"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("users", "must_change_password", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
