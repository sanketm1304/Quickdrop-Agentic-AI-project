"""The dispute catalog must remain executable and payment assertions must fail closed."""

import json
from pathlib import Path

import pytest

from evals.run import (
    CATALOG_LEDGER_CHECKS,
    COMMON_LEDGER_CHECKS,
    _catalog_checks,
    _catalog_ledger_ok,
)
from evals.scenarios import catalog_scenarios_from_manifest

CATALOG = Path(__file__).resolve().parents[2] / "scripts" / "dispute_scenarios.json"
REQUIRED_HTTP = {
    "multiple_disputed_trips",
    "duplicate_complaints",
    "changed_claimed_amount",
    "changed_trip_identifier",
    "repeat_daily_claim",
    "payment_timeout",
    "suspicious_trip_ownership",
}


def _catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_every_catalog_only_case_has_an_http_message_sequence() -> None:
    scenarios = catalog_scenarios_from_manifest(_catalog())
    assert {scenario.requirement for scenario in scenarios} == REQUIRED_HTTP
    assert all(scenario.messages for scenario in scenarios)
    assert all(scenario.rider_id.startswith("R") for scenario in scenarios)
    assert all("baseline_amount" in scenario.ground_truth for scenario in scenarios)
    assert all("baseline_provider_status" in scenario.ground_truth for scenario in scenarios)
    assert set(CATALOG_LEDGER_CHECKS) == REQUIRED_HTTP


def test_missing_catalog_message_cannot_be_marked_executable() -> None:
    catalog = _catalog()
    target = next(row for row in catalog["scenarios"] if "golden_case" not in row)
    target.pop("message_sequence")
    with pytest.raises(ValueError, match="needs a message sequence"):
        catalog_scenarios_from_manifest(catalog)


def test_catalog_payment_check_rejects_overpayment_in_provider_ledger() -> None:
    scenario = next(
        item
        for item in catalog_scenarios_from_manifest(_catalog())
        if item.requirement == "changed_claimed_amount"
    )
    steps = [
        {"reply": "Processed", "webhook_latency_ms": 5.0, "events": []} for _ in scenario.messages
    ]
    transactions = [
        {
            "idempotency_key": f"base:EP:{scenario.rider_id}:{scenario.business_date}",
            "earning_period": scenario.business_date.isoformat(),
            "simulated": True,
        },
        {
            "idempotency_key": "QD-AUTH-too-large",
            "status": "CONFIRMED",
            "amount": "120.01",
            "simulated": True,
        },
    ]
    checks = _catalog_checks(scenario, steps, transactions, set())
    assert not checks["provider_correction_within_entitlement"]
    assert not checks["settled_transfer_not_rewritten"]


def test_catalog_ledger_score_fails_on_error_or_missing_observation() -> None:
    requirement = "duplicate_complaints"
    required = (*COMMON_LEDGER_CHECKS, CATALOG_LEDGER_CHECKS[requirement])
    case = {
        "requirement": requirement,
        "checks": {name: True for name in required},
        "errors": [],
    }
    assert _catalog_ledger_ok(case)
    case["errors"].append("provider request timed out")
    assert not _catalog_ledger_ok(case)
    case["errors"].clear()
    case["checks"].pop("single_verified_transfer")
    assert not _catalog_ledger_ok(case)


def test_catalog_checks_detect_changed_baseline_and_unattributed_payment() -> None:
    scenario = next(
        item
        for item in catalog_scenarios_from_manifest(_catalog())
        if item.requirement == "suspicious_trip_ownership"
    )
    baseline = {
        "payment_id": "baseline-1",
        "idempotency_key": "base:EP:R0001:2026-10-17",
        "rider_id": scenario.rider_id,
        "earning_period": scenario.business_date.isoformat(),
        "amount": scenario.ground_truth["baseline_amount"],
        "status": "CONFIRMED",
        "currency": "INR",
        "simulated": True,
    }
    steps = [{"reply": "No account match", "webhook_latency_ms": 5.0, "events": []}]
    mutated = {**baseline, "status": "FAILED"}
    rogue = {
        "payment_id": "rogue-1",
        "idempotency_key": "rogue-key",
        "rider_id": scenario.rider_id,
        "amount": "1.00",
        "status": "CONFIRMED",
        "simulated": True,
    }
    checks = _catalog_checks(
        scenario,
        steps,
        [mutated, rogue],
        set(),
        initial_transactions=[baseline],
    )
    assert not checks["provider_baseline_matches_truth"]
    assert not checks["provider_baselines_unchanged"]
    assert not checks["provider_no_unattributed_transactions"]
