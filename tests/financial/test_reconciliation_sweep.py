"""Every settled correction must eventually be rechecked for provider changes."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.integrations.mockpayswift_client import ProviderPayment
from app.models import Conversation, Dispute, Message, PaymentAuthorization, Rider
from app.policy import POLICY_VERSION
from app.services import payments


class ProviderWithLateReversal:
    def __init__(self, reversed_key: str) -> None:
        self.reversed_key = reversed_key
        self.lookups: Counter[str] = Counter()

    async def by_key(self, key: str) -> ProviderPayment:
        self.lookups[key] += 1
        return ProviderPayment(
            payment_id=f"P-{key}",
            idempotency_key=key,
            rider_id="R1",
            dispute_reference=key.removeprefix("QD-AUTH-"),
            amount=Decimal("10.00"),
            status="REVERSED" if key == self.reversed_key else "CONFIRMED",
        )

    async def submit(self, **_kwargs) -> ProviderPayment:
        raise AssertionError("a confirmed authorization must never be submitted again")


@pytest.mark.asyncio
async def test_reconciliation_rotates_beyond_first_hundred_and_detects_reversal(
    monkeypatch,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(payments, "SessionLocal", maker)
    monkeypatch.setattr(payments, "_reconcile_cursor", None)
    try:
        async with maker.begin() as session:
            session.add(
                Rider(
                    id="R1",
                    display_name="Synthetic Rider",
                    phone_identifier="SIM-TEST-R1",
                    city="Fiction City",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(Conversation(id="C1", rider_id="R1"))
            await session.flush()
            session.add(
                Message(
                    message_id="M1",
                    rider_id="R1",
                    conversation_id="C1",
                    sender="rider",
                    text="Please review payouts",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                )
            )
            await session.flush()
            start = datetime(2026, 10, 1, tzinfo=UTC)
            for number in range(105):
                dispute_id = f"D{number:03d}"
                session.add(
                    Dispute(
                        id=dispute_id,
                        rider_id="R1",
                        conversation_id="C1",
                        source_message_id="M1",
                        category="total_payout",
                        status="PAID",
                    )
                )
                session.add(
                    PaymentAuthorization(
                        id=f"A{number:03d}",
                        dispute_id=dispute_id,
                        rider_id="R1",
                        business_date=date(2026, 10, 18),
                        mode="HUMAN",
                        status="CONFIRMED",
                        amount=Decimal("10.00"),
                        policy_version=POLICY_VERSION,
                        idempotency_key=f"QD-AUTH-{dispute_id}",
                        updated_at=start + timedelta(seconds=number),
                    )
                )
        provider = ProviderWithLateReversal("QD-AUTH-D104")

        assert await payments.reconcile_existing(provider, limit=100) == 100
        assert provider.lookups[provider.reversed_key] == 0
        async with maker() as session:
            assert (await session.get(PaymentAuthorization, "A104")).status == "CONFIRMED"

        assert await payments.reconcile_existing(provider, limit=100) == 100
        assert provider.lookups[provider.reversed_key] == 1
        async with maker() as session:
            assert (await session.get(PaymentAuthorization, "A104")).status == "OPS_REVIEW"
            assert (await session.get(Dispute, "D104")).status == "RECONCILIATION_REVIEW"
    finally:
        await engine.dispose()
