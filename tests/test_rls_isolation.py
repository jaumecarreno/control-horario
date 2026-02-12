from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.mark.integration
def test_rls_blocks_cross_tenant_reads():
    import psycopg

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set.")

    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", test_database_url)
    command.upgrade(alembic_config, "head")

    dsn = _psycopg_url(test_database_url)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if cur.fetchone()[0]:
                pytest.skip("RLS is bypassed for PostgreSQL superusers; use a non-superuser app role.")

            cur.execute(
                """
                TRUNCATE TABLE
                    audit_log,
                    shifts,
                    leave_requests,
                    leave_types,
                    time_adjustments,
                    time_events,
                    memberships,
                    employees,
                    sites,
                    users,
                    tenants
                RESTART IDENTITY CASCADE
                """
            )
            conn.commit()

            cur.execute(Path("scripts/rls.sql").read_text(encoding="utf-8"))
            conn.commit()

            tenant_a = uuid.uuid4()
            tenant_b = uuid.uuid4()
            employee_a = uuid.uuid4()
            employee_b = uuid.uuid4()

            cur.execute(
                """
                INSERT INTO tenants (id, name, slug, payroll_cutoff_day)
                VALUES (%s, %s, %s, 1), (%s, %s, %s, 1)
                """,
                (tenant_a, "Tenant A", "tenant-a-it", tenant_b, "Tenant B", "tenant-b-it"),
            )
            cur.execute(
                """
                INSERT INTO employees (id, tenant_id, name, email, pin_hash, active)
                VALUES (%s, %s, %s, NULL, NULL, TRUE), (%s, %s, %s, NULL, NULL, TRUE)
                """,
                (employee_a, tenant_a, "Alice", employee_b, tenant_b, "Bob"),
            )
            conn.commit()

            with conn.transaction():
                cur.execute(f"SET LOCAL app.tenant_id = '{tenant_a}'")
                cur.execute("SELECT COUNT(*) FROM employees")
                visible_count = cur.fetchone()[0]
                assert visible_count == 1

                cur.execute("SELECT COUNT(*) FROM employees WHERE tenant_id = %s", (tenant_b,))
                forbidden_count = cur.fetchone()[0]
                assert forbidden_count == 0
