"""Synthetic source data must reconcile exactly with the documented policy."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.policy import daily_incentive, expected_earnings, money, trip_earnings
from scripts.generate_data import (
    DEFAULT_RIDERS,
    generate_period,
    generate_rider,
    generate_riders,
    golden_manifest,
)


def test_default_generator_has_requested_volume_and_stable_ids():
    riders = periods = trips = 0
    seen_ids = set()
    for plan in generate_riders(DEFAULT_RIDERS):
        riders += 1
        for period in plan.periods:
            periods += 1
            trips += len(period.trips)
            seen_ids.update(trip["id"] for trip in period.trips)
    assert riders == 5000
    assert periods == 10000
    assert trips >= 50000
    assert len(seen_ids) == trips
    assert generate_rider(1) == generate_rider(1)
    assert generate_rider(1).rider["phone_identifier"].startswith("SIM-RIDER-")


def test_source_components_incentives_and_payout_allocations_agree():
    for number in range(1, 25):
        for day_number in (1, 2):
            plan = generate_period(number, day_number)
            trip_values = [
                trip_earnings(trip["distance_km"], trip["status"], trip["surge_category"])
                for trip in plan.trips
            ]
            completed = sum(trip["status"] == "COMPLETED" for trip in plan.trips)
            deduction = sum((row["amount"] for row in plan.deductions), Decimal(0))
            expected = expected_earnings(trip_values, daily_incentive(completed), deduction)
            assert plan.period["expected_amount"] == expected
            assert (
                sum((row["amount"] for row in plan.components), Decimal(0)) - deduction == expected
            )
            component_ids = {row["id"] for row in plan.components}
            assert all(row["component_id"] in component_ids for row in plan.allocations)
            allocated = money(sum((row["amount"] for row in plan.allocations), Decimal(0)))
            assert allocated == plan.payout["amount"]
            assert Decimal(plan.provider_payment["amount"]) == allocated
            assert plan.provider_payment["dispute_reference"] == plan.payout["id"]
            assert plan.provider_payment["idempotency_key"] == plan.payout["idempotency_key"]


def test_golden_manifest_and_threshold_boundaries():
    manifest = golden_manifest()
    saved = json.loads(
        (Path(__file__).parents[2] / "scripts" / "golden_manifest.json").read_text(encoding="utf-8")
    )
    assert saved == manifest
    cases = {case["rider_id"]: case for case in manifest["cases"]}
    assert cases["R0001"]["verified_outstanding"] == "30.00"
    assert cases["R0002"]["verified_outstanding"] == "50.00"
    assert cases["R0003"]["verified_outstanding"] == "0.00"
    assert cases["R0004"]["expected_route"] == "REVIEW"
    assert cases["R0005"]["verified_outstanding"] == "120.00"
    assert cases["R0010"]["verified_outstanding"] == "200.00"
    assert cases["R0011"]["verified_outstanding"] == "200.01"
    assert cases["R0012"]["verified_outstanding"] == "30.00"
    assert cases["R0007"]["provider_status"] == "PENDING"
    assert cases["R0008"]["provider_status"] == "REVERSED"
    assert cases["R0009"]["provider_status"] == "FAILED"
