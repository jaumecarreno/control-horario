"""Add reasons, approval comments and attachments to requests.

Revision ID: 0008_request_comments_attachments
Revises: 0007_add_punch_corrections
Create Date: 2026-02-19
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_request_comments_attachments"
down_revision: str | None = "0007_add_punch_corrections"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leave_requests", sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")))
    op.add_column("leave_requests", sa.Column("approver_comment", sa.Text(), nullable=True))
    op.add_column("leave_requests", sa.Column("attachment_name", sa.String(length=255), nullable=True))
    op.add_column("leave_requests", sa.Column("attachment_mime", sa.String(length=128), nullable=True))
    op.add_column("leave_requests", sa.Column("attachment_blob", sa.LargeBinary(), nullable=True))
    op.alter_column("leave_requests", "reason", server_default=None)

    op.add_column("punch_correction_requests", sa.Column("approver_comment", sa.Text(), nullable=True))
    op.add_column("punch_correction_requests", sa.Column("attachment_name", sa.String(length=255), nullable=True))
    op.add_column("punch_correction_requests", sa.Column("attachment_mime", sa.String(length=128), nullable=True))
    op.add_column("punch_correction_requests", sa.Column("attachment_blob", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("punch_correction_requests", "attachment_blob")
    op.drop_column("punch_correction_requests", "attachment_mime")
    op.drop_column("punch_correction_requests", "attachment_name")
    op.drop_column("punch_correction_requests", "approver_comment")

    op.drop_column("leave_requests", "attachment_blob")
    op.drop_column("leave_requests", "attachment_mime")
    op.drop_column("leave_requests", "attachment_name")
    op.drop_column("leave_requests", "approver_comment")
    op.drop_column("leave_requests", "reason")
