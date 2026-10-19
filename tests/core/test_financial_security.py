"""Financial and identity regressions against the real service code."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import agent_tools
from app.agent.schemas import IssueCategory, IssueRevision, IssueState, ToolCall
from app.database import Base
from app.integrations.mockpayswift_client import ProviderPayment, ProviderUnavailable
from app.models import (
    Conversation,
    Dispute,
    EarningAdjustmentAllocation,
    EarningComponent,
    EarningPeriod,
    Job,
    Message,
    PaymentAuthorization,
    PayoutAllocation,
    PayoutRecord,
    Rider,
    Trip,
)
from app.policy import (
    POLICY_VERSION,
    business_date,
    daily_incentive,
    money,
    requires_human,
    trip_earnings,
)
from app.services import financial, messages, payments


class FakeProvider:
    def __init__(self):
        self.payments: dict[str, ProviderPayment] = {}
        self.submit_count = 0

    async def by_key(self, key: str) -> ProviderPayment | None:
        return self.payments.get(key)

    async def submit(self, **kwargs) -> ProviderPayment:
        self.submit_count += 1
        payment = ProviderPayment(
            payment_id=f"P-{self.submit_count}",
            idempotency_key=kwargs["key"],
            rider_id=kwargs["rider_id"],
            dispute_reference=kwargs["dispute_reference"],
            amount=money(kwargs["amount"]),
            status="PENDING",
        )
        self.payments[kwargs["key"]] = payment
        return payment


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(payments, "SessionLocal", sessions)
    monkeypatch.setattr(messages, "SessionLocal", sessions)
    monkeypatch.setattr(agent_tools, "SessionLocal", sessions)
    yield sessions
    await engine.dispose()


async def seed_period(sessions, shortfall: Decimal = Decimal("60.00")):
    async with sessions.begin() as session:
        session.add_all(
            [
                Rider(
                    id="R1",
                    display_name="Synthetic One",
                    phone_identifier="demo:R1",
                    city="Fiction City",
                    status="ACTIVE",
                ),
                Rider(
                    id="R2",
                    display_name="Synthetic Two",
                    phone_identifier="demo:R2",
                    city="Fiction City",
                    status="ACTIVE",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [Conversation(id="CV1", rider_id="R1"), Conversation(id="CV2", rider_id="R2")]
        )
        await session.flush()
        session.add_all(
            [
                Message(
                    message_id="M1",
                    rider_id="R1",
                    conversation_id="CV1",
                    sender="rider",
                    text="payout short",
                    reply="checking",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                ),
                Message(
                    message_id="M2",
                    rider_id="R1",
                    conversation_id="CV1",
                    sender="rider",
                    text="same payout short",
                    reply="checking",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                ),
            ]
        )
        session.add(
            EarningPeriod(
                id="EP1",
                rider_id="R1",
                business_date=date(2026, 10, 17),
                expected_amount=money(Decimal("40.00") + shortfall),
                policy_version=POLICY_VERSION,
            )
        )
        session.add(
            Trip(
                id="T100",
                rider_id="R1",
                trip_date=date(2026, 10, 17),
                distance_km=Decimal(0),
                status="COMPLETED",
                surge_category="NONE",
                base_pay=Decimal(40),
                distance_pay=Decimal(0),
                surge_pay=Decimal(0),
                total_entitlement=Decimal(40),
                policy_version=POLICY_VERSION,
            )
        )
        await session.flush()
        session.add_all(
            [
                EarningComponent(
                    id="EC-BASE",
                    period_id="EP1",
                    trip_id="T100",
                    kind="BASE",
                    amount=Decimal("40.00"),
                    policy_version=POLICY_VERSION,
                ),
                EarningComponent(
                    id="EC-INC",
                    period_id="EP1",
                    trip_id=None,
                    kind="INCENTIVE",
                    amount=shortfall,
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
                Dispute(
                    id="D1",
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M1",
                    period_id="EP1",
                    category="total_payout",
                    claimed_amount=shortfall,
                    status="VERIFIED",
                    revision=1,
                ),
                Dispute(
                    id="D2",
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M2",
                    period_id="EP1",
                    category="total_payout",
                    claimed_amount=shortfall,
                    status="VERIFIED",
                    revision=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            PayoutAllocation(
                id="PA1", payout_record_id="BP1", component_id="EC-BASE", amount=Decimal("40.00")
            )
        )


def confirmed_baseline() -> ProviderPayment:
    return ProviderPayment(
        payment_id="PROV-BP1",
        idempotency_key="BASE-BP1",
        rider_id="R1",
        dispute_reference="BP1",
        amount=Decimal("40.00"),
        status="CONFIRMED",
    )


def test_auto_limit_exact_boundary_and_business_date():
    assert not requires_human(Decimal("199.99"), False, False)
    assert not requires_human(Decimal("200.00"), False, False)
    assert requires_human(Decimal("200.01"), False, False)
    assert requires_human(Decimal("50.00"), True, False)
    assert requires_human(Decimal("50.00"), False, True)
    assert business_date(datetime(2026, 10, 17, 19, 0, tzinfo=UTC)) == date(2026, 10, 18)
    with pytest.raises(ValueError):
        business_date(datetime(2026, 10, 18, 0, 0))


def test_source_earnings_rounding_and_incentive_steps():
    trip = trip_earnings(Decimal("1.235"), "COMPLETED", "HIGH")
    assert trip.base == Decimal("40.00")
    assert trip.distance == Decimal("9.88")
    assert trip.surge == Decimal("30.00")
    assert trip.total == Decimal("79.88")
    assert trip_earnings(Decimal("5.00"), "CANCELLED", "HIGH").total == Decimal("0.00")
    assert [daily_incentive(count) for count in (9, 10, 20)] == [
        Decimal("0.00"),
        Decimal("50.00"),
        Decimal("100.00"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shortfall", "expected"),
    [(Decimal("200.00"), "RESERVED"), (Decimal("200.01"), "REQUIRES_APPROVAL")],
)
async def test_reservation_auto_limit_boundary(db, shortfall, expected):
    await seed_period(db, shortfall)
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    outcome, amount = await payments.reserve_correction("D1", provider, mode="AUTO")
    assert outcome == expected
    assert amount == shortfall
    async with db() as session:
        auths = (await session.scalars(select(PaymentAuthorization))).all()
        assert len(auths) == (1 if expected == "RESERVED" else 0)


@pytest.mark.asyncio
async def test_provider_not_local_cache_is_settlement_authority(db):
    await seed_period(db)
    provider = FakeProvider()
    async with db() as session:
        unobserved = await financial.verify_period(session, "EP1", provider)
    assert unobserved.uncertain
    assert unobserved.confirmed_paid == Decimal("0.00")
    assert unobserved.reason and "baseline payout" in unobserved.reason
    provider.payments["BASE-BP1"] = confirmed_baseline()
    async with db.begin() as session:
        payout = await session.get(PayoutRecord, "BP1")
        payout.cached_status = "FAILED"
    async with db() as session:
        observed = await financial.verify_period(session, "EP1", provider)
    assert not observed.uncertain
    assert observed.confirmed_paid == Decimal("40.00")
    assert observed.available == Decimal("60.00")


@pytest.mark.asyncio
async def test_provider_rider_mismatch_is_uncertain(db):
    await seed_period(db)
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = ProviderPayment(
        payment_id="PROV-BP1",
        idempotency_key="BASE-BP1",
        rider_id="R2",
        dispute_reference="BP1",
        amount=Decimal("40.00"),
        status="CONFIRMED",
    )
    async with db() as session:
        result = await financial.verify_period(session, "EP1", provider)
    assert result.uncertain
    assert result.confirmed_paid == Decimal("0.00")
    assert any(item["provider_status"] == "MISMATCH" for item in result.evidence)


@pytest.mark.asyncio
async def test_overlapping_disputes_share_one_component_reservation(db):
    await seed_period(db)
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    first, amount = await payments.reserve_correction("D1", provider, mode="AUTO")
    assert (first, amount) == ("RESERVED", Decimal("60.00"))
    second, remaining = await payments.reserve_correction("D2", provider, mode="AUTO")
    # The first correction is still reserved and absent from provider history;
    # the second claim must wait for authoritative settlement evidence.
    assert (second, remaining) == ("UNCERTAIN", None)
    async with db() as session:
        auths = (await session.scalars(select(PaymentAuthorization))).all()
        allocations = (await session.scalars(select(EarningAdjustmentAllocation))).all()
        assert len(auths) == 1
        assert len(allocations) == 1
        assert allocations[0].component_id == "EC-INC"
        assert allocations[0].amount == Decimal("60.00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shortfall", "provider_status", "expected_status", "expected_available"),
    [
        ("90.00", "CONFIRMED", "PENDING_APPROVAL", "30.00"),
        ("60.00", "CONFIRMED", "NO_BALANCE", "0.00"),
        ("90.00", "UNKNOWN", "UNDER_REVIEW", "0.00"),
    ],
)
async def test_corrected_settled_claim_is_reverified_without_auto_payment(
    db, shortfall, provider_status, expected_status, expected_available
):
    await seed_period(db, Decimal(shortfall))
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    provider.payments["CORR-D1"] = ProviderPayment(
        payment_id="PROV-CORR-D1",
        idempotency_key="CORR-D1",
        rider_id="R1",
        dispute_reference="D1",
        amount=Decimal("60.00"),
        status=provider_status,
    )
    async with db.begin() as session:
        dispute = await session.get(Dispute, "D1")
        dispute.status = "PAID"
        dispute.confirmed_paid_amount = Decimal("60.00")
        session.add(
            PaymentAuthorization(
                id="AUTH-D1",
                dispute_id="D1",
                rider_id="R1",
                business_date=date(2026, 10, 18),
                mode="AUTO",
                status="CONFIRMED",
                amount=Decimal("60.00"),
                policy_version=POLICY_VERSION,
                idempotency_key="CORR-D1",
                provider_payment_id="PROV-CORR-D1",
            )
        )
        await session.flush()
        session.add(
            EarningAdjustmentAllocation(
                authorization_id="AUTH-D1",
                component_id="EC-INC",
                amount=Decimal("60.00"),
                status="CONFIRMED",
            )
        )
    issue = IssueState(
        issue_id="D1",
        category=IssueCategory.TOTAL_PAYOUT,
        earning_date=date(2026, 10, 17),
        original_claimed_amount=Decimal("60.00"),
        claimed_amount=Decimal(shortfall),
        source_message_ids=["M1", "M2"],
        revisions=[
            IssueRevision(
                message_id="M2",
                previous={"claimed_amount": "60.00"},
                current={"claimed_amount": shortfall},
                reason="correction",
            )
        ],
        requires_review=True,
    )
    async with db() as session:
        message = await session.scalar(select(Message).where(Message.message_id == "M2"))
    reply = await messages._investigate(issue, message, provider, model_review=False)
    async with db() as session:
        dispute = await session.get(Dispute, "D1")
        auths = (await session.scalars(select(PaymentAuthorization))).all()
        assert dispute.status == expected_status
        assert dispute.available_to_authorize == Decimal(expected_available)
        assert dispute.confirmed_paid_amount == Decimal("60.00")
        assert len(auths) == 1
        assert dispute.revision == 2
    assert provider.submit_count == 0
    if expected_status == "PENDING_APPROVAL":
        assert "employee approval" in reply.lower()
    elif expected_status == "NO_BALANCE":
        assert "no unpaid balance" in reply.lower()
    else:
        assert "employee will review" in reply.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shortfall", "provider_status", "expected_status", "expected_outstanding"),
    [
        ("60.00", "PENDING", "UNDER_REVIEW", "100.00"),
        ("0.00", "CONFIRMED", "NO_BALANCE", "0.00"),
    ],
)
async def test_delayed_payout_checks_provider_without_authorizing(
    db, shortfall, provider_status, expected_status, expected_outstanding
):
    await seed_period(db, Decimal(shortfall))
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = ProviderPayment(
        payment_id="PROV-BP1",
        idempotency_key="BASE-BP1",
        rider_id="R1",
        dispute_reference="BP1",
        amount=Decimal("40.00"),
        status=provider_status,
    )
    issue = IssueState(
        issue_id="D3",
        category=IssueCategory.DELAYED_PAYOUT,
        earning_date=date(2026, 10, 17),
        source_message_ids=["M1"],
    )
    async with db() as session:
        message = await session.scalar(select(Message).where(Message.message_id == "M1"))
    reply = await messages._investigate(issue, message, provider, model_review=False)
    async with db() as session:
        dispute = await session.get(Dispute, "D3")
        assert dispute.status == expected_status
        assert dispute.verified_outstanding == Decimal(expected_outstanding)
        assert dispute.available_to_authorize == Decimal("0.00")
        assert not (await session.scalars(select(PaymentAuthorization))).all()
    assert provider.submit_count == 0
    assert "simulated" in reply.lower()


@pytest.mark.asyncio
async def test_simple_status_request_uses_immediate_deterministic_path(db):
    await seed_period(db)
    reply = await messages.enqueue_message(
        message_id="M3",
        rider_id="R1",
        text="Status?",
        received_at=datetime.now(UTC),
    )
    assert "Latest recorded case status" in reply
    async with db() as session:
        message = await session.scalar(select(Message).where(Message.message_id == "M3"))
        jobs = (await session.scalars(select(Job))).all()
        assert message.status == "PROCESSED"
        assert message.reply == reply
        assert jobs == []


@pytest.mark.asyncio
async def test_status_request_waits_for_older_message_then_avoids_agent(db):
    await seed_period(db)
    async with db.begin() as session:
        older = await session.scalar(select(Message).where(Message.message_id == "M1"))
        session.add(
            Job(
                kind="MESSAGE",
                key="MESSAGE:M1",
                rider_id="R1",
                source_sequence=older.sequence,
                payload={"message_id": "M1"},
                status="READY",
            )
        )
    ack = await messages.enqueue_message(
        message_id="M3",
        rider_id="R1",
        text="Payout status",
        received_at=datetime.now(UTC),
    )
    assert ack == messages.ACK
    async with db.begin() as session:
        older_job = await session.scalar(select(Job).where(Job.key == "MESSAGE:M1"))
        older_job.status = "DONE"
    reply = await messages.process_message("M3", FakeProvider(), None)
    assert "Latest recorded case status" in reply
    async with db() as session:
        message = await session.scalar(select(Message).where(Message.message_id == "M3"))
        assert message.status == "PROCESSED"
        assert message.reply == reply


@pytest.mark.asyncio
async def test_tool_executor_does_not_reveal_other_rider_trip(db):
    await seed_period(db)
    executor = agent_tools.make_tool_executor(FakeProvider())
    hidden = await executor.execute(
        "R2", ToolCall(name="get_trip_details", arguments={"trip_id": "T100"})
    )
    assert hidden.success and hidden.result == {"found": False}
    forged = await executor.execute(
        "R2", ToolCall(name="get_trip_details", arguments={"trip_id": "T100", "rider_id": "R1"})
    )
    assert not forged.success
    assert forged.result == {"error": "invalid_arguments"}
    own = await executor.execute(
        "R1", ToolCall(name="get_trip_details", arguments={"trip_id": "T100"})
    )
    assert own.success and own.result["found"] is True


@pytest.mark.asyncio
async def test_base_pay_tool_ignores_unpaid_distance_for_same_trip(db):
    await seed_period(db)
    async with db.begin() as session:
        component = await session.get(EarningComponent, "EC-INC")
        component.kind = "DISTANCE"
        component.trip_id = "T100"
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    executor = agent_tools.make_tool_executor(provider)
    result = await executor.execute(
        "R1",
        ToolCall(
            name="calculate_verified_outstanding",
            arguments={"category": "base_pay", "trip_id": "T100"},
        ),
    )
    assert result.success
    assert result.result["issue_outstanding"] == "0"
    assert result.result["available"] == "60.00"


@pytest.mark.asyncio
async def test_only_one_auto_reservation_across_rider_periods_on_same_business_day(db):
    await seed_period(db)
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    async with db.begin() as session:
        session.add(
            Message(
                message_id="M3",
                rider_id="R1",
                conversation_id="CV1",
                sender="rider",
                text="another day",
                reply="checking",
                received_at=datetime.now(UTC),
                status="PROCESSED",
            )
        )
        session.add(
            EarningPeriod(
                id="EP2",
                rider_id="R1",
                business_date=date(2026, 10, 16),
                expected_amount=Decimal("100.00"),
                policy_version=POLICY_VERSION,
            )
        )
        await session.flush()
        session.add_all(
            [
                EarningComponent(
                    id="EC2-BASE",
                    period_id="EP2",
                    kind="BASE",
                    amount=Decimal("40.00"),
                    policy_version=POLICY_VERSION,
                ),
                EarningComponent(
                    id="EC2-INC",
                    period_id="EP2",
                    kind="INCENTIVE",
                    amount=Decimal("60.00"),
                    policy_version=POLICY_VERSION,
                ),
                PayoutRecord(
                    id="BP2",
                    rider_id="R1",
                    period_id="EP2",
                    payout_batch_id="BATCH2",
                    idempotency_key="BASE-BP2",
                    amount=Decimal("40.00"),
                ),
                Dispute(
                    id="D3",
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M3",
                    period_id="EP2",
                    category="total_payout",
                    claimed_amount=Decimal("60.00"),
                    status="VERIFIED",
                    revision=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            PayoutAllocation(
                id="PA2",
                payout_record_id="BP2",
                component_id="EC2-BASE",
                amount=Decimal("40.00"),
            )
        )
    provider.payments["BASE-BP2"] = ProviderPayment(
        payment_id="PROV-BP2",
        idempotency_key="BASE-BP2",
        rider_id="R1",
        dispute_reference="BP2",
        amount=Decimal("40.00"),
        status="CONFIRMED",
    )
    assert (await payments.reserve_correction("D1", provider, mode="AUTO"))[0] == "RESERVED"
    outcome, amount = await payments.reserve_correction("D3", provider, mode="AUTO")
    assert outcome == "REQUIRES_APPROVAL"
    assert amount == Decimal("60.00")
    async with db() as session:
        assert len((await session.scalars(select(PaymentAuthorization))).all()) == 1


@pytest.mark.asyncio
async def test_timeout_after_provider_creation_keeps_reservation_until_confirmation(db):
    await seed_period(db)
    provider = FakeProvider()
    provider.payments["BASE-BP1"] = confirmed_baseline()
    assert (await payments.reserve_correction("D1", provider, mode="AUTO"))[0] == "RESERVED"
    async with db() as session:
        auth = (await session.scalars(select(PaymentAuthorization))).one()
        auth_id, key = auth.id, auth.idempotency_key

    class TimeoutAfterCreate(FakeProvider):
        def __init__(self, baseline):
            super().__init__()
            self.payments["BASE-BP1"] = baseline

        async def submit(self, **kwargs):
            await super().submit(**kwargs)
            raise ProviderUnavailable("lost HTTP response")

    uncertain_provider = TimeoutAfterCreate(confirmed_baseline())
    result = await payments.execute_payment(auth_id, uncertain_provider)
    assert result == "RECONCILING"
    assert uncertain_provider.submit_count == 1
    async with db() as session:
        auth = await session.get(PaymentAuthorization, auth_id)
        dispute = await session.get(Dispute, "D1")
        allocation = (await session.scalars(select(EarningAdjustmentAllocation))).one()
        assert auth.status == "RECONCILING"
        assert dispute.status == "RECONCILING"
        assert dispute.confirmed_paid_amount == Decimal("0.00")
        assert allocation.status == "RESERVED"
    created = uncertain_provider.payments[key]
    uncertain_provider.payments[key] = ProviderPayment(
        payment_id=created.payment_id,
        idempotency_key=created.idempotency_key,
        rider_id=created.rider_id,
        dispute_reference=created.dispute_reference,
        amount=created.amount,
        status="CONFIRMED",
    )
    assert await payments.execute_payment(auth_id, uncertain_provider) == "CONFIRMED"
    assert uncertain_provider.submit_count == 1
    async with db() as session:
        dispute = await session.get(Dispute, "D1")
        assert dispute.status == "PAID"
        assert dispute.confirmed_paid_amount == Decimal("60.00")
