"""A corrected trip reference must recheck source records before reservation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import agent_tools
from app.config import Settings
from app.database import Base
from app.integrations.mockpayswift_client import ProviderPayment
from app.models import AuditEvent, Dispute, EarningAdjustmentAllocation, PaymentAuthorization
from app.services import messages, payments
from scripts.generate_data import generate_rider
from scripts.seed_database import seed_operational_data


class BaselineProvider:
    def __init__(self) -> None:
        self.payments = {}
        for period in generate_rider(1).periods:
            payout = period.payout
            self.payments[payout["idempotency_key"]] = ProviderPayment(
                payment_id=f"PROV-{payout['id']}",
                idempotency_key=payout["idempotency_key"],
                rider_id="R0001",
                dispute_reference=payout["id"],
                amount=payout["amount"],
                status="CONFIRMED",
            )

    async def by_key(self, key):
        return self.payments.get(key)

    async def rider_transactions(self, rider_id, limit=50):
        return [payment for payment in self.payments.values() if payment.rider_id == rider_id][
            :limit
        ]

    async def submit(self, **_kwargs):
        raise AssertionError("message processing must only reserve, never submit payment")


@pytest.mark.asyncio
async def test_corrected_trip_after_no_balance_reserves_only_real_missing_surge(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(messages, "SessionLocal", sessions)
    monkeypatch.setattr(payments, "SessionLocal", sessions)
    monkeypatch.setattr(agent_tools, "SessionLocal", sessions)
    provider = BaselineProvider()
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        session_secret="test-only-session-secret-32-characters-long",
        webhook_token="test-only-webhook-token",
        mockpayswift_api_key="test-only-provider-token",
        agent_mode="offline",
    )
    try:
        await seed_operational_data(
            engine,
            rider_count=1,
            rider_password="synthetic-rider-password",
            employee_password="synthetic-employee-password",
        )
        for message_id, text in (
            ("M1", "Trip T0001202 surge missing"),
            ("M2", "Sorry, I meant T0001201"),
        ):
            await messages.enqueue_message(
                message_id=message_id,
                rider_id="R0001",
                text=text,
                received_at=datetime(2026, 10, 18, tzinfo=UTC),
            )
            await messages.process_message(message_id, provider, settings)
        async with sessions() as session:
            disputes = (await session.scalars(select(Dispute))).all()
            authorizations = (await session.scalars(select(PaymentAuthorization))).all()
            allocations = (await session.scalars(select(EarningAdjustmentAllocation))).all()
            events = (await session.scalars(select(AuditEvent))).all()
        assert len(disputes) == 1
        assert disputes[0].trip_id == "T0001201"
        assert disputes[0].revision == 2
        assert len(authorizations) == 1, {
            "disputes": [
                (row.category, row.status, row.explanation, str(row.available_to_authorize))
                for row in disputes
            ],
            "tools": [event.details for event in events if event.event_type == "AGENT_TOOL"],
        }
        assert authorizations[0].amount == Decimal("30.00")
        assert len(allocations) == 1
        assert allocations[0].component_id == "C:T0001201:SURGE"
    finally:
        await engine.dispose()
