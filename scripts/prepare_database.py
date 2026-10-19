"""Prepare the restricted application role and migrate the operational database.

This process alone receives the PostgreSQL owner password. It runs once before
the API and worker start, and is safe to rerun against an existing Compose volume.
"""

from __future__ import annotations

import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

APP_ROLE = "quickdrop_app"
SEED_ROLE = "quickdrop_seed"
RESET_FUNCTION = "public.quickdrop_test_reset_fixtures()"
SOURCE_TABLES = (
    "riders",
    "employees",
    "trips",
    "earning_periods",
    "earning_components",
    "deductions",
    "payout_records",
    "payout_allocations",
)
MUTABLE_TABLES = (
    "conversations",
    "messages",
    "disputes",
    "dispute_revisions",
    "dispute_evidence",
    "payment_authorizations",
    "earning_adjustment_allocations",
    "payment_attempts",
    "payment_reconciliations",
    "approvals",
    "jobs",
    "outbox_events",
)


def _required_secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value or value.startswith("replace-with-"):
        raise ValueError(f"{name} must be set to a non-placeholder secret")
    return value


def _test_mode() -> bool:
    return os.getenv("QUICKDROP_TEST_MODE", "").lower() in {"1", "true", "yes", "on"}


async def _ensure_roles(owner_url: URL, app_password: str, seed_password: str) -> None:
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            for role, password in ((APP_ROLE, app_password), (SEED_ROLE, seed_password)):
                exists = await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"),
                    {"name": role},
                )
                action = "ALTER" if exists else "CREATE"
                role_sql = await connection.scalar(
                    text(
                        "SELECT format('"
                        + action
                        + " ROLE "
                        + role
                        + " WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS PASSWORD %L', CAST(:password AS text))"
                    ),
                    {"password": password},
                )
                await connection.exec_driver_sql(role_sql)
    finally:
        await engine.dispose()


async def _grant_runtime_access(owner_url: URL, *, test_mode: bool) -> None:
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            statements = (
                "REVOKE CREATE, TEMP ON DATABASE quickdrop FROM PUBLIC",
                "REVOKE ALL ON DATABASE quickdrop FROM quickdrop_app",
                "GRANT CONNECT ON DATABASE quickdrop TO quickdrop_app",
                "REVOKE ALL ON DATABASE quickdrop FROM quickdrop_seed",
                "GRANT CONNECT ON DATABASE quickdrop TO quickdrop_seed",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                "REVOKE ALL ON SCHEMA public FROM quickdrop_app",
                "GRANT USAGE ON SCHEMA public TO quickdrop_app",
                "REVOKE ALL ON SCHEMA public FROM quickdrop_seed",
                "GRANT USAGE ON SCHEMA public TO quickdrop_seed",
                "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM quickdrop_app",
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO quickdrop_app",
                "REVOKE ALL ON TABLE public.alembic_version FROM quickdrop_app",
                "GRANT INSERT ON TABLE public.audit_events TO quickdrop_app",
                "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM quickdrop_seed",
                "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM quickdrop_app",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quickdrop_app",
                "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM quickdrop_seed",
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE quickdrop IN SCHEMA public "
                    "REVOKE INSERT, UPDATE, DELETE ON TABLES FROM quickdrop_app"
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE quickdrop IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO quickdrop_app"
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE quickdrop IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO quickdrop_app"
                ),
            )
            for statement in statements:
                await connection.exec_driver_sql(statement)
            await connection.exec_driver_sql(
                "GRANT INSERT, UPDATE, DELETE ON TABLE "
                + ", ".join(f"public.{table}" for table in MUTABLE_TABLES)
                + " TO quickdrop_app"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT ON TABLE "
                + ", ".join(f"public.{table}" for table in SOURCE_TABLES)
                + " TO quickdrop_seed"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (provider_payment_id, cached_status) "
                "ON TABLE public.payout_records TO quickdrop_seed"
            )

            if test_mode:
                await connection.exec_driver_sql("""
                    CREATE OR REPLACE FUNCTION public.quickdrop_test_reset_fixtures()
                    RETURNS void
                    LANGUAGE plpgsql
                    SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $$
                    BEGIN
                        TRUNCATE TABLE
                            public.payment_reconciliations,
                            public.payment_attempts,
                            public.earning_adjustment_allocations,
                            public.approvals,
                            public.dispute_evidence,
                            public.dispute_revisions,
                            public.payment_authorizations,
                            public.audit_events,
                            public.jobs,
                            public.outbox_events,
                            public.disputes,
                            public.messages,
                            public.conversations
                        RESTART IDENTITY CASCADE;
                    END;
                    $$
                """)
                await connection.exec_driver_sql(
                    f"REVOKE ALL ON FUNCTION {RESET_FUNCTION} FROM PUBLIC"
                )
                await connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {RESET_FUNCTION} TO quickdrop_app"
                )
            else:
                # Remove a function left by a volume previously used in test mode.
                await connection.exec_driver_sql(f"DROP FUNCTION IF EXISTS {RESET_FUNCTION}")
    finally:
        await engine.dispose()


def main() -> None:
    owner_password = _required_secret("POSTGRES_PASSWORD")
    app_password = _required_secret("APP_DATABASE_PASSWORD")
    seed_password = _required_secret("SEED_DATABASE_PASSWORD")
    owner_url = URL.create(
        "postgresql+asyncpg",
        username="quickdrop",
        password=owner_password,
        host="db",
        port=5432,
        database="quickdrop",
    )
    asyncio.run(_ensure_roles(owner_url, app_password, seed_password))

    # Alembic reads this only in the isolated setup process. Runtime containers
    # receive neither the owner URL nor the owner password.
    os.environ["MIGRATION_DATABASE_URL"] = owner_url.render_as_string(hide_password=False)
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(_grant_runtime_access(owner_url, test_mode=_test_mode()))


if __name__ == "__main__":
    main()
