"""Conversation ordering, rejected follow-up, and provider-outage integration cases."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import agent_tools
from app.config import Settings
from app.database import Base
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import Conversation, Dispute, DisputeRevision, Job, Message, PaymentAuthorization
from app.services import messages, payments
from app.workers import worker
from mockpayswift.config import ProviderSettings
from mockpayswift.main import create_app
from scripts.seed_database import seed_operational_data, seed_provider_payments

RECEIVED = datetime(2026, 10, 18, 10, tzinfo=UTC)


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    app_url = f"sqlite+aiosqlite:///{(tmp_path / 'app.sqlite3').as_posix()}"
    engine = create_async_engine(app_url)

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_operational_data(
        engine,
        rider_count=1,
        rider_password="synthetic-rider-password",
        employee_password="synthetic-employee-password",
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
        database_url=app_url,
        session_secret="test-only-session-secret-32-characters-long",
        webhook_token="test-only-webhook-token",
        mockpayswift_api_key="provider-secret",
        mockpayswift_url="http://provider",
        agent_mode="offline",
    )
    provider = MockPaySwiftClient(settings, transport=transport)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(messages, "SessionLocal", sessions)
    monkeypatch.setattr(payments, "SessionLocal", sessions)
    monkeypatch.setattr(agent_tools, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    yield sessions, provider, transport
    await engine.dispose()


async def send(message_id, text, when=RECEIVED):
    return await messages.enqueue_message(
        message_id=message_id, rider_id="R0001", text=text, received_at=when
    )


@pytest.mark.asyncio
async def test_c01_three_messages_clarify_then_report_case_status(environment):
    sessions, provider, _ = environment
    assert await send("M1", "My payout is short") == messages.ACK
    assert await worker.run_once(provider)
    async with sessions() as session:
        dispute = (await session.scalars(select(Dispute))).one()
        assert dispute.status == "NEEDS_CLARIFICATION"
        original_id = dispute.id

    assert await send("M2", "Sorry, the payout for 2026-10-17 is short") == messages.ACK
    assert await worker.run_once(provider)
    status_reply = await send("M3", "Status?")
    assert "Latest recorded case status" in status_reply

    async with sessions() as session:
        dispute = (await session.scalars(select(Dispute))).one()
        incoming = (await session.scalars(select(Message).order_by(Message.sequence))).all()
        authorizations = (await session.scalars(select(PaymentAuthorization))).all()
        assert dispute.id == original_id
        assert dispute.revision == 2
        assert dispute.earning_date.isoformat() == "2026-10-17"
        assert dispute.status == "PROCESSING"
        assert len(authorizations) == 1 and authorizations[0].amount == Decimal("30.00")
        assert [item.message_id for item in incoming] == ["M1", "M2", "M3"]
        assert all(item.status == "PROCESSED" for item in incoming)
        assert "simulated correction processing" in incoming[2].reply
    ledger = await provider.rider_transactions("R0001")
    assert len([payment for payment in ledger if payment.dispute_reference == original_id]) == 0


@pytest.mark.asyncio
async def test_c08_rejected_dispute_followup_does_not_reopen_or_pay(environment):
    sessions, provider, _ = environment
    async with sessions.begin() as session:
        session.add(Conversation(id="C1", rider_id="R0001"))
        session.add(
            Message(
                message_id="M0",
                rider_id="R0001",
                conversation_id="C1",
                sender="rider",
                text="Trip T0001201 surge missing",
                received_at=RECEIVED,
                reply="This claim was rejected after review.",
                status="PROCESSED",
            )
        )
        await session.flush()
        session.add(
            Dispute(
                id="D-REJECTED",
                rider_id="R0001",
                conversation_id="C1",
                source_message_id="M0",
                period_id="EP:R0001:2026-10-17",
                trip_id="T0001201",
                earning_date=RECEIVED.date() - timedelta(days=1),
                category="surge",
                status="REJECTED",
                revision=1,
                explanation="Reviewed and rejected by operations.",
            )
        )
        await session.flush()
        session.add(
            DisputeRevision(
                dispute_id="D-REJECTED",
                message_id="M0",
                revision=1,
                trip_id="T0001201",
                category="surge",
            )
        )

    assert await send("M1", "Trip T0001201 surge missing") == messages.ACK
    assert await worker.run_once(provider)
    async with sessions() as session:
        disputes = (await session.scalars(select(Dispute))).all()
        authorizations = (await session.scalars(select(PaymentAuthorization))).all()
        followup = await session.scalar(select(Message).where(Message.message_id == "M1"))
        assert len(disputes) == 1
        assert disputes[0].id == "D-REJECTED" and disputes[0].status == "REJECTED"
        assert authorizations == []
        assert "already rejected" in followup.reply.lower()
    ledger = await provider.rider_transactions("R0001")
    assert not [payment for payment in ledger if payment.dispute_reference == "D-REJECTED"]


@pytest.mark.asyncio
async def test_c10_out_of_order_timestamps_do_not_bypass_rider_job_sequence(environment):
    sessions, provider, _ = environment
    assert await send("M1", "Trip T0001202 surge missing", RECEIVED) == messages.ACK
    assert (
        await send("M2", "Sorry, I meant T0001201", RECEIVED - timedelta(hours=1)) == messages.ACK
    )
    async with sessions.begin() as session:
        jobs = (await session.scalars(select(Job).where(Job.kind == "MESSAGE"))).all()
        by_message = {job.payload["message_id"]: job for job in jobs}
        by_message["M1"].run_at = datetime.now(UTC) - timedelta(seconds=10)
        by_message["M2"].run_at = datetime.now(UTC) - timedelta(seconds=20)
    assert await worker.run_once(provider)
    async with sessions() as session:
        first = await session.scalar(select(Message).where(Message.message_id == "M1"))
        second = await session.scalar(select(Message).where(Message.message_id == "M2"))
        assert first.status == "PROCESSED" and second.status == "QUEUED"
    assert await worker.run_once(provider)
    async with sessions() as session:
        messages_rows = (await session.scalars(select(Message).order_by(Message.sequence))).all()
        dispute = (await session.scalars(select(Dispute))).one()
        authorizations = (await session.scalars(select(PaymentAuthorization))).all()
        assert [row.status for row in messages_rows] == ["PROCESSED", "PROCESSED"]
        assert dispute.trip_id == "T0001201" and dispute.revision == 2
        assert len(authorizations) == 1 and authorizations[0].amount == Decimal("30.00")


@pytest.mark.asyncio
async def test_s11_provider_outage_does_not_authorize_or_claim_settlement(environment):
    sessions, provider, transport = environment
    headers = {"Authorization": "Bearer provider-secret", "X-Test-Token": "test-secret"}
    async with httpx.AsyncClient(
        base_url="http://provider", headers=headers, transport=transport
    ) as client:
        outage = await client.post("/test/outage", json={"unavailable": True})
        assert outage.status_code == 204
    assert await send("M1", "Trip T0001201 surge missing") == messages.ACK
    assert await worker.run_once(provider)
    async with sessions() as session:
        dispute = (await session.scalars(select(Dispute))).one()
        authorizations = (await session.scalars(select(PaymentAuthorization))).all()
        message = await session.scalar(select(Message).where(Message.message_id == "M1"))
        assert dispute.status == "UNDER_REVIEW"
        assert dispute.available_to_authorize == Decimal("0.00")
        assert not authorizations
        assert "unclear" in message.reply.lower()
        dispute_id = dispute.id
    async with httpx.AsyncClient(
        base_url="http://provider", headers=headers, transport=transport
    ) as client:
        restored = await client.post("/test/outage", json={"unavailable": False})
        assert restored.status_code == 204
    ledger = await provider.rider_transactions("R0001")
    assert not [payment for payment in ledger if payment.dispute_reference == dispute_id]
