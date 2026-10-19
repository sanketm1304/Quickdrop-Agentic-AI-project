"""The dispute catalog must point to real, internally consistent source records."""

import json
from decimal import Decimal
from pathlib import Path

from scripts.generate_data import DEFAULT_SEED, generate_period, golden_manifest

CATALOG = Path(__file__).resolve().parents[2] / "scripts" / "dispute_scenarios.json"
REQUIRED = {
    "valid_missing_surge",
    "valid_missing_incentive",
    "incorrect_total_payout",
    "fully_paid_trip",
    "partial_payment",
    "multiple_disputed_trips",
    "duplicate_complaints",
    "overlapping_claims",
    "changed_claimed_amount",
    "changed_trip_identifier",
    "repeat_daily_claim",
    "high_value_claim",
    "payment_failure",
    "payment_timeout",
    "reversed_payment",
    "suspicious_trip_ownership",
}


def _unpaid_trip(plan, trip_id: str) -> Decimal:
    allocations = {row["component_id"]: row["amount"] for row in plan.allocations}
    return sum(
        (
            component["amount"] - allocations.get(component["id"], Decimal(0))
            for component in plan.components
            if component["trip_id"] == trip_id
        ),
        Decimal(0),
    )


def test_dispute_catalog_matches_synthetic_ground_truth() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert catalog["generator_seed"] == DEFAULT_SEED
    cases = catalog["scenarios"]
    assert {case["requirement"] for case in cases} == REQUIRED
    golden = {case["rider_id"]: case for case in golden_manifest()["cases"]}

    for case in cases:
        rider_id = case["source_rider_id"]
        plan = generate_period(int(rider_id[1:]), 2)
        assert plan.period["rider_id"] == rider_id
        if case["requirement"] != "suspicious_trip_ownership":
            assert all(
                trip_id in {row["id"] for row in plan.trips} for trip_id in case["source_trip_ids"]
            )
        assert all(
            component_id in {row["id"] for row in plan.components}
            for component_id in case.get("source_component_ids", [])
        )
        if "source_payout_id" in case:
            assert case["source_payout_id"] == plan.payout["id"]
        if "golden_case" in case:
            expected = golden[case["golden_case"]]
            assert case["golden_case"] == rider_id
            truth = case["ground_truth"]
            if "outstanding" in truth:
                assert Decimal(truth["outstanding"]) == Decimal(expected["verified_outstanding"])
            if "route" in truth:
                assert truth["route"] == expected["expected_route"]
            if "baseline_provider_status" in truth:
                assert truth["baseline_provider_status"] == expected["provider_status"]

    by_name = {case["requirement"]: case for case in cases}
    multiple = by_name["multiple_disputed_trips"]
    plan = generate_period(4, 2)
    assert {
        trip_id: Decimal(amount)
        for trip_id, amount in multiple["ground_truth"]["unpaid_by_trip"].items()
    } == {trip_id: _unpaid_trip(plan, trip_id) for trip_id in multiple["source_trip_ids"]}
    assert sum(
        (Decimal(value) for value in multiple["ground_truth"]["unpaid_by_trip"].values()),
        Decimal(0),
    ) == Decimal(multiple["ground_truth"]["aggregate_outstanding"])

    repeat = by_name["repeat_daily_claim"]
    assert _unpaid_trip(plan, repeat["source_trip_ids"][0]) == Decimal(
        repeat["ground_truth"]["first_trip_outstanding"]
    )
    assert _unpaid_trip(plan, repeat["source_trip_ids"][1]) == Decimal(
        repeat["ground_truth"]["second_trip_outstanding"]
    )

    changed = by_name["changed_trip_identifier"]
    plan = generate_period(1, 2)
    assert _unpaid_trip(plan, changed["source_trip_ids"][0]) == Decimal(
        changed["ground_truth"]["first_trip_outstanding"]
    )
    assert _unpaid_trip(plan, changed["source_trip_ids"][1]) == Decimal(
        changed["ground_truth"]["corrected_trip_outstanding"]
    )

    ownership = by_name["suspicious_trip_ownership"]
    assert ownership["source_trip_ids"][0] in {row["id"] for row in generate_period(2, 2).trips}
    assert ownership["actual_trip_owner_id"] != ownership["source_rider_id"]
