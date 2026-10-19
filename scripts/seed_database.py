"""Idempotently load synthetic operations data and MockPaySwift baseline payouts."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings
from app.security import hash_password
from scripts.generate_data import (
    BUSINESS_DATES,
    DEFAULT_RIDERS,
    DEFAULT_SEED,
    GOLDEN_RIDERS,
    baseline_status,
    generate_riders,
)


@dataclass(frozen=True)
class SeedSummary:
    riders: int
    periods: int
    trips: int
    components: int
    deductions: int
    payout_records: int
    payout_allocations: int


def _chunks(values: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _insert_if_missing(session, table, rows: list[dict], dialect_name: str) -> None:
    if not rows:
        return
    if dialect_name == "postgresql":
        statement = postgresql_insert(table)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(table)
    else:
        raise ValueError(f"unsupported seed database dialect: {dialect_name}")
    primary_key = [column.name for column in table.primary_key.columns]
    await session.execute(statement.on_conflict_do_nothing(index_elements=primary_key), rows)


async def seed_operational_data(
    engine: AsyncEngine,
    *,
    rider_count: int = DEFAULT_RIDERS,
    seed: int = DEFAULT_SEED,
    rider_password: str,
    employee_password: str,
    batch_riders: int = 50,
) -> SeedSummary:
    """Insert source records in FK order; rerunning preserves existing IDs and decisions."""
    from app.models import (
        Deduction,
        EarningComponent,
        EarningPeriod,
        Employee,
        PayoutAllocation,
        PayoutRecord,
        Rider,
        Trip,
    )

    if rider_count < 1 or rider_count > 9999 or batch_riders < 1:
        raise ValueError("invalid rider count or batch size")
    rider_hash = hash_password(rider_password)
    employee_hash = hash_password(employee_password)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker.begin() as session:
        await _insert_if_missing(
            session,
            Employee.__table__,
            [
                {
                    "id": "OPS001",
                    "display_name": "Synthetic Operations Reviewer",
                    "role": "reviewer",
                    "password_hash": employee_hash,
                    "active": True,
                },
                {
                    "id": "OPS002",
                    "display_name": "Synthetic Operations Administrator",
                    "role": "admin",
                    "password_hash": employee_hash,
                    "active": True,
                },
            ],
            engine.dialect.name,
        )
    counts = {
        "riders": 0,
        "periods": 0,
        "trips": 0,
        "components": 0,
        "deductions": 0,
        "payout_records": 0,
        "payout_allocations": 0,
    }
    for plans in _chunks(generate_riders(rider_count, seed=seed), batch_riders):
        rows: dict[str, list[dict]] = {
            key: []
            for key in (
                "riders",
                "periods",
                "trips",
                "components",
                "deductions",
                "payout_records",
                "payout_allocations",
            )
        }
        for plan in plans:
            rider = dict(plan.rider)
            if int(rider["id"][1:]) in GOLDEN_RIDERS:
                rider["password_hash"] = rider_hash
            rows["riders"].append(rider)
            for period in plan.periods:
                rows["periods"].append(period.period)
                rows["trips"].extend(period.trips)
                rows["components"].extend(period.components)
                rows["deductions"].extend(period.deductions)
                rows["payout_records"].append(period.payout)
                rows["payout_allocations"].extend(period.allocations)
        async with maker.begin() as session:
            for key, table in (
                ("riders", Rider.__table__),
                ("trips", Trip.__table__),
                ("periods", EarningPeriod.__table__),
                ("components", EarningComponent.__table__),
                ("deductions", Deduction.__table__),
                ("payout_records", PayoutRecord.__table__),
                ("payout_allocations", PayoutAllocation.__table__),
            ):
                await _insert_if_missing(session, table, rows[key], engine.dialect.name)
                counts[key] += len(rows[key])
    return SeedSummary(**counts)


async def _send_provider_batches(
    engine: AsyncEngine,
    payment_pairs: Iterable[tuple[str, dict[str, str]]],
    *,
    provider_url: str,
    api_key: str,
    test_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
    update_cache: bool = True,
) -> dict[str, int]:
    """Send atomic batches through the authenticated provider fixture API."""
    from app.models import PayoutRecord

    if not api_key or not test_token:
        raise ValueError("provider API key and test token are required for fixture seeding")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    created = replayed = 0
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Test-Token": test_token,
    }
    async with httpx.AsyncClient(
        base_url=provider_url,
        headers=headers,
        timeout=httpx.Timeout(60.0),
        transport=transport,
    ) as client:
        for batch in _chunks(payment_pairs, 1000):
            key_to_payout = {payment["idempotency_key"]: payout_id for payout_id, payment in batch}
            if len(key_to_payout) != len(batch):
                raise RuntimeError("duplicate provider idempotency key in seed batch")
            response = await client.post(
                "/test/seed-payments", json={"payments": [payment for _, payment in batch]}
            )
            response.raise_for_status()
            result = response.json()
            if result.get("created", 0) + result.get("replayed", 0) != len(batch):
                raise RuntimeError("provider seed batch count mismatch")
            items = result.get("items")
            if not isinstance(items, list) or len(items) != len(batch):
                raise RuntimeError("provider seed response lacks payment references")
            returned_keys = {item.get("idempotency_key") for item in items}
            if set(key_to_payout) != returned_keys:
                raise RuntimeError("provider seed returned mismatched payment keys")
            updates = [
                {
                    "id": key_to_payout[item["idempotency_key"]],
                    "provider_payment_id": item["payment_id"],
                    "cached_status": item["status"],
                }
                for item in items
            ]
            if update_cache:
                async with maker.begin() as session:
                    await session.execute(update(PayoutRecord), updates)
            created += result["created"]
            replayed += result["replayed"]
    return {"created": created, "replayed": replayed}


async def seed_provider_payments(
    engine: AsyncEngine,
    *,
    provider_url: str,
    api_key: str,
    test_token: str,
    rider_count: int = DEFAULT_RIDERS,
    seed: int = DEFAULT_SEED,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    """Load generated baseline payouts through HTTP; no direct provider DB access."""
    payment_pairs = (
        (period.payout["id"], period.provider_payment)
        for rider in generate_riders(rider_count, seed=seed)
        for period in rider.periods
    )
    return await _send_provider_batches(
        engine,
        payment_pairs,
        provider_url=provider_url,
        api_key=api_key,
        test_token=test_token,
        transport=transport,
    )


async def seed_provider_from_db(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    update_cache: bool = True,
) -> dict[str, int]:
    """Restore all synthetic baseline payouts after a guarded provider reset.

    This reads QuickDrop's payout records and calls MockPaySwift over HTTP. The
    provider's SQLite database is never opened by QuickDrop.
    """
    from app.models import EarningPeriod, PayoutRecord

    owned_engine = engine is None
    if engine is None:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                await session.execute(
                    select(PayoutRecord, EarningPeriod)
                    .join(EarningPeriod, PayoutRecord.period_id == EarningPeriod.id)
                    .where(PayoutRecord.id.like("PAYOUT:%"))
                    .order_by(PayoutRecord.id)
                )
            ).all()
        payment_pairs = []
        for payout, period in rows:
            try:
                number = int(payout.rider_id.removeprefix("R"))
            except ValueError as exc:
                raise ValueError(f"invalid synthetic rider ID: {payout.rider_id}") from exc
            day_number = 2 if period.business_date == BUSINESS_DATES[1] else 1
            payment_pairs.append(
                (
                    payout.id,
                    {
                        "idempotency_key": payout.idempotency_key,
                        "rider_id": payout.rider_id,
                        "dispute_reference": payout.id,
                        "amount": str(payout.amount),
                        "currency": "INR",
                        "source_ref": period.id,
                        "earning_period": period.business_date.isoformat(),
                        "status": baseline_status(number, day_number),
                    },
                )
            )
        return await _send_provider_batches(
            engine,
            payment_pairs,
            provider_url=settings.mockpayswift_url,
            api_key=settings.mockpayswift_api_key,
            test_token=settings.mockpayswift_test_token,
            transport=transport,
            update_cache=update_cache,
        )
    finally:
        if owned_engine:
            await engine.dispose()


async def bootstrap(
    settings: Settings,
    *,
    rider_count: int = DEFAULT_RIDERS,
    seed: int = DEFAULT_SEED,
    skip_provider: bool = False,
) -> tuple[SeedSummary, dict[str, int] | None]:
    if not settings.demo_rider_password or not settings.demo_employee_password:
        raise ValueError("DEMO_RIDER_PASSWORD and DEMO_EMPLOYEE_PASSWORD are required")
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        operational = await seed_operational_data(
            engine,
            rider_count=rider_count,
            seed=seed,
            rider_password=settings.demo_rider_password,
            employee_password=settings.demo_employee_password,
        )
        provider = None
        if not skip_provider:
            provider = await seed_provider_payments(
                engine,
                provider_url=settings.mockpayswift_url,
                api_key=settings.mockpayswift_api_key,
                test_token=settings.mockpayswift_test_token,
                rider_count=rider_count,
                seed=seed,
            )
        return operational, provider
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently seed QuickDrop and MockPaySwift")
    parser.add_argument("--riders", type=int, default=DEFAULT_RIDERS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--skip-provider", action="store_true")
    args = parser.parse_args()
    operational, provider = asyncio.run(
        bootstrap(
            Settings(), rider_count=args.riders, seed=args.seed, skip_provider=args.skip_provider
        )
    )
    print(f"Operational synthetic rows: {operational}")
    if provider is not None:
        print(f"MockPaySwift baseline payments: {provider}")


if __name__ == "__main__":
    main()
