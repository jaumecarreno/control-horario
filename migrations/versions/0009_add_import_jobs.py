"""Add import jobs table for bulk onboarding workflow.

Revision ID: 0009_add_import_jobs
Revises: 0008_request_comments_attachments
Create Date: 2026-02-20
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0009_add_import_jobs"
down_revision: str | None = "0008_request_comments_attachments"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


import_job_status = postgresql.ENUM(
    "PREVIEWED",
    "COMMITTED",
    "EXPIRED",
    name="import_job_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        import_job_status.create(bind, checkfirst=True)

    status_column = (
        import_job_status
        if bind.dialect.name == "postgresql"
        else sa.Enum("PREVIEWED", "COMMITTED", "EXPIRED", name="import_job_status")
    )

    op.create_table(
        "import_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", status_column, nullable=False, server_default=sa.text("'PREVIEWED'")),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("rows_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json" if bind.dialect.name == "postgresql" else "'[]'")),
        sa.Column("errors_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json" if bind.dialect.name == "postgresql" else "'[]'")),
        sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json" if bind.dialect.name == "postgresql" else "'{}'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_jobs_tenant_status", "import_jobs", ["tenant_id", "status"], unique=False)
    op.create_index("ix_import_jobs_tenant_expires", "import_jobs", ["tenant_id", "expires_at"], unique=False)

    op.alter_column("import_jobs", "status", server_default=None)
    op.alter_column("import_jobs", "rows_json", server_default=None)
    op.alter_column("import_jobs", "errors_json", server_default=None)
    op.alter_column("import_jobs", "summary_json", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_import_jobs_tenant_expires", table_name="import_jobs")
    op.drop_index("ix_import_jobs_tenant_status", table_name="import_jobs")
    op.drop_table("import_jobs")

    if bind.dialect.name == "postgresql":
        import_job_status.drop(bind, checkfirst=True)
