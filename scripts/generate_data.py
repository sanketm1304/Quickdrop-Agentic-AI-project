"""Deterministic, internally consistent QuickDrop synthetic source records."""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.policy import POLICY_VERSION, daily_incentive, expected_earnings, money, trip_earnings

DEFAULT_SEED = 20261018
DEFAULT_RIDERS = 5000
BUSINESS_DATES = (date(2026, 10, 16), date(2026, 10, 17))
GOLDEN_RIDERS = frozenset(range(1, 13))
GENERATOR_VERSION = "synthetic-v1"
SURGE_CATEGORIES = ("NONE", "NONE", "NONE", "STANDARD", "STANDARD", "HIGH")
CITIES = ("Bengaluru", "Delhi", "Hyderabad", "Mumbai", "Pune", "Chennai")


@dataclass(frozen=True)
class PeriodPlan:
    period: dict[str, Any]
    trips: tuple[dict[str, Any], ...]
    components: tuple[dict[str, Any], ...]
    deductions: tuple[dict[str, Any], ...]
    payout: dict[str, Any]
    allocations: tuple[dict[str, Any], ...]
    provider_payment: dict[str, str]


@dataclass(frozen=True)
class RiderPlan:
    rider: dict[str, Any]
    periods: tuple[PeriodPlan, ...]


def rider_id(number: int) -> str:
    return f"R{number:04d}"


def trip_id(number: int, day_number: int, trip_number: int) -> str:
    return f"T{number:04d}{day_number}{trip_number:02d}"


def _rng(seed: int, number: int, day_number: int) -> random.Random:
    return random.Random(seed + number * 1009 + day_number * 65537)


def _trip_count(number: int, day_number: int) -> int:
    if day_number == 2 and number in {1, 2, 4, 10, 11}:
        return 10
    return 6


def baseline_status(number: int, day_number: int) -> str:
    if day_number == 2:
        return {7: "PENDING", 8: "REVERSED", 9: "FAILED"}.get(number, "CONFIRMED")
    return "CONFIRMED"


def _missing_component_ids(number: int, day_number: int, components: list[dict]) -> set[str]:
    if day_number != 2:
        return set()
    if number in {1, 12}:
        first_trip = trip_id(number, day_number, 1)
        return {c["id"] for c in components if c["trip_id"] == first_trip and c["kind"] == "SURGE"}
    if number == 2:
        return {c["id"] for c in components if c["kind"] == "INCENTIVE"}
    if number == 4:
        first_three = {trip_id(number, day_number, n) for n in range(1, 4)}
        return {c["id"] for c in components if c["trip_id"] in first_three}
    if number == 5:
        first_three = {trip_id(number, day_number, n) for n in range(1, 4)}
        return {c["id"] for c in components if c["trip_id"] in first_three and c["kind"] == "BASE"}
    return set()


def _withhold_exact(
    paid_by_component: dict[str, Decimal], components: list[dict], amount: Decimal
) -> None:
    remaining = money(amount)
    for component in components:
        component_id = component["id"]
        withheld = min(paid_by_component.get(component_id, money(0)), remaining)
        paid_by_component[component_id] -= withheld
        remaining -= withheld
        if remaining == 0:
            return
    raise ValueError(f"insufficient source entitlement for exact shortfall {amount}")


def generate_period(number: int, day_number: int, *, seed: int = DEFAULT_SEED) -> PeriodPlan:
    if day_number not in (1, 2):
        raise ValueError("day_number must be 1 or 2")
    if number < 1 or number > 9999:
        raise ValueError("rider number must be 1..9999")
    business_date = BUSINESS_DATES[day_number - 1]
    rid = rider_id(number)
    period_id = f"EP:{rid}:{business_date.isoformat()}"
    payout_id = f"PAYOUT:{rid}:{business_date.isoformat()}"
    randomizer = _rng(seed, number, day_number)
    count = _trip_count(number, day_number)
    trips: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    trip_values = []
    completed_count = 0
    for trip_number in range(1, count + 1):
        tid = trip_id(number, day_number, trip_number)
        status = "COMPLETED"
        if number not in GOLDEN_RIDERS and trip_number == count and randomizer.randrange(8) == 0:
            status = "CANCELLED"
        distance_hundredths = randomizer.randint(150, 1100)
        if number == 6 and day_number == 2 and trip_number == 1:
            distance_hundredths = 400
        distance = Decimal(distance_hundredths) / Decimal(100)
        surge_category = randomizer.choice(SURGE_CATEGORIES)
        if day_number == 2 and number in {1, 12} and trip_number == 1:
            surge_category = "HIGH"
        values = trip_earnings(distance, status, surge_category)
        trips.append(
            {
                "id": tid,
                "rider_id": rid,
                "trip_date": business_date,
                "distance_km": distance,
                "status": status,
                "surge_category": surge_category,
                "base_pay": values.base,
                "distance_pay": values.distance,
                "surge_pay": values.surge,
                "total_entitlement": values.total,
                "policy_version": POLICY_VERSION,
            }
        )
        if status == "COMPLETED":
            completed_count += 1
            for kind, amount in (
                ("BASE", values.base),
                ("DISTANCE", values.distance),
                ("SURGE", values.surge),
            ):
                if amount > 0:
                    components.append(
                        {
                            "id": f"C:{tid}:{kind}",
                            "period_id": period_id,
                            "trip_id": tid,
                            "kind": kind,
                            "amount": amount,
                            "policy_version": POLICY_VERSION,
                        }
                    )
        trip_values.append(values)
    incentive = daily_incentive(completed_count)
    for increment in range(1, int(incentive / Decimal(50)) + 1):
        components.append(
            {
                "id": f"C:{period_id}:INCENTIVE:{increment:02d}",
                "period_id": period_id,
                "trip_id": None,
                "kind": "INCENTIVE",
                "amount": money(50),
                "policy_version": POLICY_VERSION,
            }
        )
    deduction_amount = money(0)
    deductions: list[dict[str, Any]] = []
    if number == 6 and day_number == 2:
        deduction_amount = money(25)
    elif number not in GOLDEN_RIDERS and randomizer.randrange(50) == 0:
        deduction_amount = money(10)
    if deduction_amount:
        deductions.append(
            {
                "id": f"DED:{period_id}:EQUIPMENT",
                "period_id": period_id,
                "rider_id": rid,
                "category": "EQUIPMENT",
                "reason": "Synthetic recorded equipment replacement",
                "evidence_reference": f"EV:{period_id}:EQUIPMENT",
                "amount": deduction_amount,
            }
        )
    expected = expected_earnings(trip_values, incentive, deduction_amount)
    assert expected == money(sum((c["amount"] for c in components), Decimal(0)) - deduction_amount)
    paid_by_component = {component["id"]: component["amount"] for component in components}
    if deduction_amount:
        first_base = next(c for c in components if c["kind"] == "BASE")
        paid_by_component[first_base["id"]] -= deduction_amount
    for component_id in _missing_component_ids(number, day_number, components):
        paid_by_component[component_id] = money(0)
    if day_number == 2 and number in {10, 11}:
        _withhold_exact(
            paid_by_component,
            components,
            money("200.00" if number == 10 else "200.01"),
        )
    allocations = [
        {
            "id": f"PA:{payout_id}:{component['id']}",
            "payout_record_id": payout_id,
            "component_id": component["id"],
            "amount": paid_by_component[component["id"]],
        }
        for component in components
        if paid_by_component[component["id"]] > 0
    ]
    payout_amount = money(sum((a["amount"] for a in allocations), Decimal(0)))
    if payout_amount <= 0 or payout_amount > expected:
        raise AssertionError("synthetic payout must be positive and within expected entitlement")
    provider_status = baseline_status(number, day_number)
    period = {
        "id": period_id,
        "rider_id": rid,
        "business_date": business_date,
        "expected_amount": expected,
        "policy_version": POLICY_VERSION,
    }
    payout = {
        "id": payout_id,
        "rider_id": rid,
        "period_id": period_id,
        "payout_batch_id": f"BATCH:{business_date.isoformat()}",
        "idempotency_key": f"base:{period_id}",
        "provider_payment_id": None,
        "amount": payout_amount,
        "cached_status": "UNKNOWN",
        "created_at": datetime.combine(business_date, datetime.min.time(), tzinfo=UTC),
    }
    provider_payment = {
        "idempotency_key": payout["idempotency_key"],
        "rider_id": rid,
        "dispute_reference": payout_id,
        "amount": str(payout_amount),
        "currency": "INR",
        "source_ref": period_id,
        "earning_period": business_date.isoformat(),
        "status": provider_status,
    }
    return PeriodPlan(
        period=period,
        trips=tuple(trips),
        components=tuple(components),
        deductions=tuple(deductions),
        payout=payout,
        allocations=tuple(allocations),
        provider_payment=provider_payment,
    )


def generate_rider(number: int, *, seed: int = DEFAULT_SEED) -> RiderPlan:
    if number < 1 or number > 9999:
        raise ValueError("rider number must be 1..9999")
    rid = rider_id(number)
    rider = {
        "id": rid,
        "display_name": f"Synthetic Rider {number:04d}",
        "phone_identifier": f"SIM-RIDER-{number:04d}",
        "city": CITIES[(number - 1) % len(CITIES)],
        "status": "ACTIVE",
        "password_hash": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return RiderPlan(
        rider=rider,
        periods=(generate_period(number, 1, seed=seed), generate_period(number, 2, seed=seed)),
    )


def generate_riders(
    count: int = DEFAULT_RIDERS, *, seed: int = DEFAULT_SEED
) -> Iterator[RiderPlan]:
    if count < 1 or count > 9999:
        raise ValueError("rider count must be 1..9999")
    for number in range(1, count + 1):
        yield generate_rider(number, seed=seed)


def golden_manifest(*, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    descriptions = {
        1: ("Missing source surge", "AUTO", "surge"),
        2: ("Missing daily incentive", "AUTO", "incentive"),
        3: ("Fully paid trip", "NO_PAYMENT", "trip"),
        4: ("High value total shortfall", "REVIEW", "total"),
        5: ("Partial base payout", "AUTO", "partial"),
        6: ("Documented deduction, net paid", "NO_PAYMENT", "deduction"),
        7: ("Pending baseline payout", "REVIEW", "delayed"),
        8: ("Reversed baseline payout", "REVIEW", "reversed"),
        9: ("Failed baseline payout", "REVIEW", "failed"),
        10: ("Exactly at auto threshold", "AUTO", "total"),
        11: ("One paise above auto threshold", "REVIEW", "total"),
        12: ("Overlapping surge and total claims", "AUTO_THEN_NO_PAYMENT", "overlap"),
    }
    cases = []
    for number, (description, expected_route, category) in descriptions.items():
        plan = generate_period(number, 2, seed=seed)
        expected = plan.period["expected_amount"]
        baseline = plan.payout["amount"]
        provider_status = plan.provider_payment["status"]
        confirmed = baseline if provider_status == "CONFIRMED" else money(0)
        cases.append(
            {
                "rider_id": rider_id(number),
                "business_date": BUSINESS_DATES[1].isoformat(),
                "trip_id": plan.trips[0]["id"],
                "description": description,
                "category": category,
                "expected_route": expected_route,
                "expected_earnings": str(expected),
                "baseline_amount": str(baseline),
                "provider_status": provider_status,
                "provider_confirmed_paid": str(confirmed),
                "verified_outstanding": str(money(expected - confirmed)),
                "uncertain": provider_status != "CONFIRMED",
            }
        )
    return {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "business_dates": [day.isoformat() for day in BUSINESS_DATES],
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview deterministic synthetic QuickDrop data")
    parser.add_argument("--riders", type=int, default=DEFAULT_RIDERS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--manifest", action="store_true", help="print the golden case manifest")
    parser.add_argument("--write-manifest", type=Path, help="write golden cases as JSON")
    args = parser.parse_args()
    if args.write_manifest:
        args.write_manifest.write_text(
            json.dumps(golden_manifest(seed=args.seed), indent=2) + "\n", encoding="utf-8"
        )
        print(f"Wrote {args.write_manifest}")
        return
    if args.manifest:
        print(json.dumps(golden_manifest(seed=args.seed), indent=2))
        return
    riders = periods = trips = components = payouts = 0
    for plan in generate_riders(args.riders, seed=args.seed):
        riders += 1
        for period in plan.periods:
            periods += 1
            trips += len(period.trips)
            components += len(period.components)
            payouts += 1
    print(
        json.dumps(
            {
                "riders": riders,
                "periods": periods,
                "trips": trips,
                "components": components,
                "payouts": payouts,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
