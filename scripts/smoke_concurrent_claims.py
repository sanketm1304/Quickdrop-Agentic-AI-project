"""Opt-in PostgreSQL race check for F03 on seeded R0004 trip base pay.

Run inside the app container after seeding and fixture reset:

    docker compose exec -T app python -m scripts.smoke_concurrent_claims

This inserts two workflow claims and allows one simulated correction. It never
changes source earnings rows. Reset fixtures before another golden evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import (
    Conversation,
    Dispute,
    EarningComponent,
    EarningPeriod,
    Message,
    PaymentAuthorization,
    Rider,
    Trip,
)
from app.policy import AUTO_LIMIT, business_date, money
from app.services.financial import select_component_allocations, verify_period
from app.services.payments import reserve_correction

RIDER_ID = "R0004"
TRIP_IDS = ("T0004201", "T0004202")
PERIOD_ID = "EP:R0004:2026-10-17"


async def _prepare(
    provider: MockPaySwiftClient,
) -> tuple[dict[str, str], dict[str, Decimal], Decimal]:
    """Check clean source/provider state, then insert only workflow fixtures."""
    async with SessionLocal() as session:
        rider = await session.get(Rider, RIDER_ID)
        period = await session.get(EarningPeriod, PERIOD_ID)
        if rider is None or period is None:
            raise AssertionError("Seed R0004 before running F03")
        if await session.scalar(
            select(PaymentAuthorization.id).where(PaymentAuthorization.rider_id == RIDER_ID)
        ):
            raise AssertionError("R0004 already has a correction; reset fixtures first")
        trips = [await session.get(Trip, trip_id) for trip_id in TRIP_IDS]
        if any(trip is None or trip.rider_id != RIDER_ID for trip in trips):
            raise AssertionError("R0004 source trips are missing or belong to another rider")
        components = (
            await session.scalars(
                select(EarningComponent).where(EarningComponent.period_id == PERIOD_ID)
            )
        ).all()
        verification = await verify_period(session, PERIOD_ID, provider)
        if verification.uncertain or verification.available <= 0:
            raise AssertionError("R0004 baseline payout is not confirmed and available")
        amounts = {
            trip_id: money(
                sum(
                    select_component_allocations(
                        components, verification, "BASE", trip_id
                    ).values(),
                    Decimal(0),
                )
            )
            for trip_id in TRIP_IDS
        }
        if any(not 0 < amount <= AUTO_LIMIT for amount in amounts.values()):
            raise AssertionError(f"Both trip base-pay claims must be auto eligible: {amounts}")
        expected = money(period.expected_amount)

    ledger = await provider.rider_transactions(RIDER_ID, limit=100)
    if any(not payment.dispute_reference.startswith(f"PAYOUT:{RIDER_ID}:") for payment in ledger):
        raise AssertionError("R0004 provider has a prior correction; reset fixtures first")
    baseline = await provider.by_key(f"base:{PERIOD_ID}")
    if baseline is None or baseline.status != "CONFIRMED":
        raise AssertionError("R0004 baseline provider transaction is not confirmed")

    suffix = uuid4().hex[:12]
    dispute_ids = {trip_id: f"F03-{suffix}-{number}" for number, trip_id in enumerate(TRIP_IDS, 1)}
    async with SessionLocal.begin() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.rider_id == RIDER_ID)
        )
        if conversation is None:
            conversation = Conversation(rider_id=RIDER_ID)
            session.add(conversation)
            await session.flush()
        for number, trip_id in enumerate(TRIP_IDS, 1):
            message_id = f"F03.{suffix}.{number}"
            session.add(
                Message(
                    message_id=message_id,
                    rider_id=RIDER_ID,
                    conversation_id=conversation.id,
                    sender="rider",
                    text=f"Trip {trip_id} base pay is missing",
                    reply="Concurrent-claim test fixture",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                )
            )
            await session.flush()
            session.add(
                Dispute(
                    id=dispute_ids[trip_id],
                    rider_id=RIDER_ID,
                    conversation_id=conversation.id,
                    source_message_id=message_id,
                    period_id=PERIOD_ID,
                    trip_id=trip_id,
                    earning_date=period.business_date,
                    category="base_pay",
                    claimed_amount=amounts[trip_id],
                    verified_outstanding=verification.verified_outstanding,
                    available_to_authorize=amounts[trip_id],
                    status="AUTO_ELIGIBLE",
                )
            )
    return dispute_ids, amounts, expected


async def smoke(timeout: float = 45) -> None:
    settings = get_settings()
    if not settings.quickdrop_test_mode or not settings.database_url.startswith("postgresql"):
        raise RuntimeError("F03 requires a test-mode PostgreSQL app container")
    provider = MockPaySwiftClient(settings)
    dispute_ids, eligible, expected = await _prepare(provider)
    gate = asyncio.Event()

    async def attempt(dispute_id: str):
        await gate.wait()
        return await reserve_correction(dispute_id, provider, mode="AUTO")

    tasks = [asyncio.create_task(attempt(dispute_ids[trip_id])) for trip_id in TRIP_IDS]
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    if any(isinstance(outcome, BaseException) for outcome in outcomes):
        raise AssertionError(f"Concurrent reservation raised an exception: {outcomes!r}")
    reserved = [index for index, outcome in enumerate(outcomes) if outcome[0] == "RESERVED"]
    if len(reserved) != 1:
        raise AssertionError(f"Exactly one claim must reserve: {outcomes!r}")
    winner_index = reserved[0]
    loser_index = 1 - winner_index
    if outcomes[loser_index][0] not in {"UNCERTAIN", "REQUIRES_APPROVAL"}:
        raise AssertionError(f"Losing claim took an unexpected route: {outcomes!r}")
    winner_trip = TRIP_IDS[winner_index]
    winner_id = dispute_ids[winner_trip]
    loser_id = dispute_ids[TRIP_IDS[loser_index]]

    async with SessionLocal() as session:
        auths = (
            await session.scalars(
                select(PaymentAuthorization).where(PaymentAuthorization.rider_id == RIDER_ID)
            )
        ).all()
    if len(auths) != 1 or auths[0].mode != "AUTO" or auths[0].dispute_id != winner_id:
        raise AssertionError(f"Only the winning AUTO authorization may exist: {auths!r}")
    auth = auths[0]
    if (
        auth.business_date != business_date(datetime.now(UTC))
        or money(auth.amount) != eligible[winner_trip]
    ):
        raise AssertionError("Winner amount or same-day allowance is incorrect")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payment = await provider.by_key(auth.idempotency_key)
        if payment is not None and payment.status == "CONFIRMED":
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("Winning simulated correction did not settle before timeout")

    follow_up = await reserve_correction(loser_id, provider, mode="AUTO")
    if follow_up[0] != "REQUIRES_APPROVAL":
        raise AssertionError(f"Second same-day AUTO claim was not blocked: {follow_up!r}")
    async with SessionLocal() as session:
        auths = (
            await session.scalars(
                select(PaymentAuthorization).where(PaymentAuthorization.rider_id == RIDER_ID)
            )
        ).all()
    if len(auths) != 1:
        raise AssertionError("A second same-day authorization appeared")

    ledger = await provider.rider_transactions(RIDER_ID, limit=100)
    correction = [item for item in ledger if item.dispute_reference in set(dispute_ids.values())]
    if len(correction) != 1 or correction[0].idempotency_key != auth.idempotency_key:
        raise AssertionError(f"Provider has duplicate or mismatched correction: {correction!r}")
    if correction[0].status != "CONFIRMED" or money(correction[0].amount) != money(auth.amount):
        raise AssertionError("Provider settlement disagrees with the authorization")
    baseline = next(
        item for item in ledger if item.dispute_reference == f"PAYOUT:{RIDER_ID}:2026-10-17"
    )
    if money(baseline.amount + correction[0].amount) > expected:
        raise AssertionError("Provider confirmed more than the source period entitlement")
    print(
        "F03 PASS: two simultaneous eligible trip claims, one AUTO authorization, "
        f"one confirmed provider correction of INR {auth.amount:.2f}; "
        "second same-day claim requires approval"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    asyncio.run(smoke(args.timeout))


if __name__ == "__main__":
    main()
