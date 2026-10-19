"""F08/S12: a fresh worker recovers durable payment work after process loss."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import (
    Conversation,
    Dispute,
    EarningComponent,
    EarningPeriod,
    Job,
    Message,
    OutboxEvent,
    PaymentAuthorization,
    PayoutAllocation,
    PayoutRecord,
    Rider,
)
from app.policy import POLICY_VERSION
from app.services import payments
from app.workers import worker
from mockpayswift.config import ProviderSettings
from mockpayswift.main import create_app


def make_app_engine(database_url):
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_provider(provider_settings, settings):
    provider_app = create_app(provider_settings)
    provider_app.state.ledger.initialize()
    return MockPaySwiftClient(settings, transport=httpx.ASGITransport(app=provider_app))


async def seed_shortfall(sessions):
    async with sessions.begin() as session:
        session.add(
            Rider(
                id="R1", display_name="Recovery Rider", phone_identifier="demo:R1", city="Test City"
            )
        )
        await session.flush()
        session.add(Conversation(id="C1", rider_id="R1"))
        session.add(
            EarningPeriod(
                id="EP1",
                rider_id="R1",
                business_date=date(2026, 10, 17),
                expected_amount=Decimal("100.00"),
                policy_version=POLICY_VERSION,
            )
        )
        await session.flush()
        session.add_all(
            [
                EarningComponent(
                    id="BASE",
                    period_id="EP1",
                    kind="BASE",
                    amount=Decimal("40.00"),
                    policy_version=POLICY_VERSION,
                ),
                EarningComponent(
                    id="INC",
                    period_id="EP1",
                    kind="INCENTIVE",
                    amount=Decimal("60.00"),
                    policy_version=POLICY_VERSION,
                ),
                PayoutRecord(
                    id="BP1",
                    rider_id="R1",
                    period_id="EP1",
                    payout_batch_id="BATCH1",
                    idempotency_key="BASE-BP1",
                    amount=Decimal("40.00"),
                    cached_status="CONFIRMED",
                ),
            ]
        )
        session.add(
            Message(
                message_id="M1",
                rider_id="R1",
                conversation_id="C1",
                sender="rider",
                text="Payout is short by Rs 60",
                received_at=datetime.now(UTC),
                status="PROCESSED",
            )
        )
        await session.flush()
        session.add(
            PayoutAllocation(
                id="PA1", payout_record_id="BP1", component_id="BASE", amount=Decimal("40.00")
            )
        )
        session.add(
            Dispute(
                id="D1",
                rider_id="R1",
                conversation_id="C1",
                source_message_id="M1",
                period_id="EP1",
                earning_date=date(2026, 10, 17),
                category="total_payout",
                claimed_amount=Decimal("60.00"),
                status="VERIFIED",
                revision=1,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_before_crash", [False, True])
async def test_f08_s12_payment_job_recovers_after_worker_and_app_restart(
    tmp_path, monkeypatch, submitted_before_crash
):
    app_url = f"sqlite+aiosqlite:///{(tmp_path / 'app.sqlite3').as_posix()}"
    provider_settings = ProviderSettings(
        str(tmp_path / "provider.sqlite3"), "provider-secret", True, "test-secret"
    )
    settings = Settings(
        database_url=app_url,
        session_secret="test-secret-at-least-32-characters-123",
        webhook_token="test-webhook",
        mockpayswift_api_key="provider-secret",
        mockpayswift_url="http://provider",
    )
    first_engine = make_app_engine(app_url)
    async with first_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    first_sessions = async_sessionmaker(first_engine, expire_on_commit=False)
    monkeypatch.setattr(payments, "SessionLocal", first_sessions)
    monkeypatch.setattr(worker, "SessionLocal", first_sessions)
    await seed_shortfall(first_sessions)
    first_provider = make_provider(provider_settings, settings)
    baseline = await first_provider.submit(
        key="BASE-BP1", rider_id="R1", dispute_reference="BP1", amount=Decimal("40.00")
    )
    assert baseline.status == "CONFIRMED"
    assert await payments.reserve_correction("D1", first_provider, mode="AUTO") == (
        "RESERVED",
        Decimal("60.00"),
    )

    claimed = await worker.claim_job()
    assert claimed is not None and claimed.kind == "PAYMENT"
    async with first_sessions() as session:
        auth = (await session.scalars(select(PaymentAuthorization))).one()
        auth_id, key = auth.id, auth.idempotency_key
        job = (await session.scalars(select(Job))).one()
        assert job.status == "RUNNING" and job.attempts == 1
    provider_payment_id = None
    if submitted_before_crash:
        # The provider commits, then the application process dies before
        # recording its response. Recovery must look up the same stable key.
        payment = await first_provider.submit(
            key=key, rider_id="R1", dispute_reference="D1", amount=Decimal("60.00")
        )
        provider_payment_id = payment.payment_id
        assert payment.status == "CONFIRMED"
    await first_engine.dispose()

    # New app engine, worker sessions, and provider process over the same
    # persisted databases model a restart after the lease has expired.
    second_engine = make_app_engine(app_url)
    second_sessions = async_sessionmaker(second_engine, expire_on_commit=False)
    monkeypatch.setattr(payments, "SessionLocal", second_sessions)
    monkeypatch.setattr(worker, "SessionLocal", second_sessions)
    second_provider = make_provider(provider_settings, settings)
    try:
        async with second_sessions.begin() as session:
            job = (await session.scalars(select(Job))).one()
            assert job.status == "RUNNING"
            job.locked_until = datetime.now(UTC) - timedelta(seconds=1)
            auth = await session.get(PaymentAuthorization, auth_id)
            assert auth.status == "RESERVED"

        assert await worker.run_once(second_provider)
        assert not await worker.run_once(second_provider)
        async with second_sessions() as session:
            job = (await session.scalars(select(Job))).one()
            auth = await session.get(PaymentAuthorization, auth_id)
            dispute = await session.get(Dispute, "D1")
            outbox = (await session.scalars(select(OutboxEvent))).one()
            assert job.status == "DONE" and job.attempts == 2
            assert auth.status == "CONFIRMED"
            assert dispute.status == "PAID"
            assert dispute.confirmed_paid_amount == Decimal("60.00")
            assert outbox.delivered_at is not None

        payment = await second_provider.by_key(key)
        assert payment is not None and payment.status == "CONFIRMED"
        if provider_payment_id is not None:
            assert payment.payment_id == provider_payment_id
        ledger = await second_provider.rider_transactions("R1")
        assert len(ledger) == 2
        assert sum((row.amount for row in ledger), Decimal(0)) == Decimal("100.00")
        assert len([row for row in ledger if row.idempotency_key == key]) == 1
    finally:
        await second_engine.dispose()
