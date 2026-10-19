"""Regression checks for provider authority over local payment flags."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mockpayswift.config import ProviderSettings
from mockpayswift.main import create_app
from scripts.seed_database import seed_operational_data, seed_provider_payments


async def _environment(tmp_path, monkeypatch, auth_status: str):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-32-characters-123")
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-webhook")
    monkeypatch.setenv("MOCKPAYSWIFT_API_KEY", "provider-secret")
    from app.config import Settings
    from app.database import Base
    from app.integrations.mockpayswift_client import MockPaySwiftClient
    from app.models import (
        Conversation,
        Dispute,
        EarningAdjustmentAllocation,
        Message,
        PaymentAuthorization,
    )

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'quickdrop.sqlite3').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_operational_data(
        engine,
        rider_count=1,
        rider_password="test-rider-password-123",
        employee_password="test-employee-password-123",
    )
    provider_app = create_app(
        ProviderSettings(str(tmp_path / "provider.sqlite3"), "provider-secret", True, "test-secret")
    )
    provider_app.state.ledger.initialize()
    transport = httpx.ASGITransport(app=provider_app)
    await seed_provider_payments(
        engine,
        provider_url="http://provider",
        api_key="provider-secret",
        test_token="test-secret",
        rider_count=1,
        transport=transport,
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'quickdrop.sqlite3').as_posix()}",
        session_secret="test-secret-at-least-32-characters-123",
        webhook_token="test-webhook",
        mockpayswift_api_key="provider-secret",
        mockpayswift_url="http://provider",
    )
    provider = MockPaySwiftClient(settings, transport=transport)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(Conversation(id="C1", rider_id="R0001"))
        session.add_all(
            [
                Message(
                    message_id=key,
                    rider_id="R0001",
                    conversation_id="C1",
                    sender="rider",
                    text="Trip surge missing",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                )
                for key in ("M1", "M2")
            ]
        )
        await session.flush()
        for key, message_id in (("D1", "M1"), ("D2", "M2")):
            session.add(
                Dispute(
                    id=key,
                    rider_id="R0001",
                    conversation_id="C1",
                    source_message_id=message_id,
                    period_id="EP:R0001:2026-10-17",
                    trip_id="T0001201",
                    category="surge",
                    status="PENDING_APPROVAL",
                    revision=1,
                )
            )
        await session.flush()
        session.add(
            PaymentAuthorization(
                id="AUTH1",
                dispute_id="D1",
                rider_id="R0001",
                business_date=date(2026, 10, 18),
                mode="HUMAN",
                status=auth_status,
                amount=Decimal("30.00"),
                policy_version="QD-2026-01",
                idempotency_key="QD-AUTH-AUTH1",
            )
        )
        await session.flush()
        session.add(
            EarningAdjustmentAllocation(
                authorization_id="AUTH1",
                component_id="C:T0001201:SURGE",
                amount=Decimal("30.00"),
                status="RESERVED" if auth_status == "RESERVED" else "RELEASED",
            )
        )
    return engine, maker, provider


@pytest.mark.asyncio
async def test_missing_provider_correction_is_uncertain(tmp_path, monkeypatch):
    from app.services.financial import verify_period

    engine, maker, provider = await _environment(tmp_path, monkeypatch, "RESERVED")
    try:
        async with maker() as session:
            verification = await verify_period(session, "EP:R0001:2026-10-17", provider)
        assert verification.uncertain is True
        assert any(
            item["kind"] == "ADJUSTMENT" and item["provider_status"] == "MISSING"
            for item in verification.evidence
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_failed_flag_cannot_override_provider_confirmation(tmp_path, monkeypatch):
    from app.models import PaymentAuthorization
    from app.services import payments

    engine, maker, provider = await _environment(tmp_path, monkeypatch, "FAILED")
    monkeypatch.setattr(payments, "SessionLocal", maker)
    try:
        previous = await provider.submit(
            key="QD-AUTH-AUTH1",
            rider_id="R0001",
            dispute_reference="D1",
            amount=Decimal("30.00"),
        )
        assert previous.status == "CONFIRMED"
        outcome, _ = await payments.reserve_correction(
            "D2", provider, mode="AUTO", expected_revision=1
        )
        assert outcome != "RESERVED"
        async with maker() as session:
            assert await session.scalar(select(func.count()).select_from(PaymentAuthorization)) == 1
    finally:
        await engine.dispose()
