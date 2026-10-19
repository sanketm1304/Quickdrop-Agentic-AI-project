"""Database and provider seeding stays repeatable across interrupted runs."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mockpayswift.config import ProviderSettings
from mockpayswift.main import create_app
from scripts.seed_database import (
    seed_operational_data,
    seed_provider_from_db,
    seed_provider_payments,
)


@pytest.mark.asyncio
async def test_idempotent_operational_and_provider_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-32-characters-123")
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-webhook")
    monkeypatch.setenv("MOCKPAYSWIFT_API_KEY", "provider-secret")
    from app.config import Settings, get_settings
    from app.database import Base
    from app.integrations.mockpayswift_client import MockPaySwiftClient
    from app.models import Employee, PayoutRecord, Rider, Trip
    from app.security import verify_password
    from app.services.financial import verify_period

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'quickdrop.sqlite3').as_posix()}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    rider_password = "demo-rider-password-123"
    employee_password = "demo-employee-password-123"
    first = await seed_operational_data(
        engine,
        rider_count=12,
        rider_password=rider_password,
        employee_password=employee_password,
    )
    assert first.riders == 12
    assert first.periods == 24
    assert first.trips > 144
    second = await seed_operational_data(
        engine,
        rider_count=12,
        rider_password=rider_password,
        employee_password=employee_password,
    )
    assert second == first
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(Rider)) == 12
        assert await session.scalar(select(func.count()).select_from(Trip)) == first.trips
        assert await session.scalar(select(func.count()).select_from(PayoutRecord)) == 24
        reviewer = await session.get(Employee, "OPS001")
        demo_rider = await session.get(Rider, "R0001")
        ordinary_rider = await session.get(Rider, "R0012")
        assert reviewer and reviewer.role == "reviewer"
        assert verify_password(employee_password, reviewer.password_hash)
        assert demo_rider and verify_password(rider_password, demo_rider.password_hash)
        assert ordinary_rider and verify_password(rider_password, ordinary_rider.password_hash)

    provider = create_app(
        ProviderSettings(
            database_path=str(tmp_path / "provider.sqlite3"),
            api_key="provider-secret",
            test_mode=True,
            test_token="test-secret",
        )
    )
    provider.state.ledger.initialize()
    transport = httpx.ASGITransport(app=provider)
    first_provider = await seed_provider_payments(
        engine,
        provider_url="http://provider",
        api_key="provider-secret",
        test_token="test-secret",
        rider_count=12,
        transport=transport,
    )
    assert first_provider == {"created": 24, "replayed": 0}
    second_provider = await seed_provider_payments(
        engine,
        provider_url="http://provider",
        api_key="provider-secret",
        test_token="test-secret",
        rider_count=12,
        transport=transport,
    )
    assert second_provider == {"created": 0, "replayed": 24}
    async with maker() as session:
        payout = await session.get(PayoutRecord, "PAYOUT:R0001:2026-10-17")
        assert payout and payout.provider_payment_id and payout.cached_status == "CONFIRMED"

    settings = Settings(
        database_url=database_url,
        session_secret="test-secret-at-least-32-characters-123",
        webhook_token="test-webhook",
        mockpayswift_api_key="provider-secret",
        mockpayswift_test_token="test-secret",
        mockpayswift_url="http://provider",
    )
    client = MockPaySwiftClient(settings, transport=transport)
    async with maker() as session:
        for number, amount, uncertain in (
            (1, "30.00", False),
            (2, "50.00", False),
            (3, "0.00", False),
            (6, "0.00", False),
            (7, None, True),
            (10, "200.00", False),
            (11, "200.01", False),
        ):
            verification = await verify_period(session, f"EP:R{number:04d}:2026-10-17", client)
            assert verification.uncertain is uncertain
            if amount is not None:
                assert verification.available == Decimal(amount)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://provider",
        headers={"Authorization": "Bearer provider-secret", "X-Test-Token": "test-secret"},
    ) as http:
        reset = await http.post("/test/reset")
        assert reset.status_code == 204
    restored = await seed_provider_from_db(settings, engine=engine, transport=transport)
    assert restored == {"created": 24, "replayed": 0}
    assert provider.state.ledger.get_by_key("base:EP:R0008:2026-10-17")["status"] == "REVERSED"
    await engine.dispose()
    get_settings.cache_clear()
