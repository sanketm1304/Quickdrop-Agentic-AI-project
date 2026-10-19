"""Run golden and dispute-catalog scenarios over HTTP; inspect MockPaySwift's ledger.

Usage from the Compose app container:
    python -m evals.run --base-url http://app:8000 \
        --provider-url http://mockpayswift:8001 --reset-fixtures

This runner does not query either database or import the financial engine. Its
expected values come from committed golden and dispute-catalog fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from evals.reporting import build_report
from evals.scenarios import (
    CatalogScenario,
    MessageStep,
    Scenario,
    catalog_scenarios_from_manifest,
    scenarios_from_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "scripts" / "golden_manifest.json"
DEFAULT_CATALOG = ROOT / "scripts" / "dispute_scenarios.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "latest.json"
REVIEW_STATES = {"PENDING_APPROVAL", "UNDER_REVIEW", "RECONCILIATION_REVIEW"}
CATALOG_LEDGER_CHECKS = {
    "multiple_disputed_trips": "single_auto_correction",
    "duplicate_complaints": "single_verified_transfer",
    "changed_claimed_amount": "settled_transfer_not_rewritten",
    "changed_trip_identifier": "only_corrected_trip_paid",
    "repeat_daily_claim": "first_auto_second_review",
    "payment_timeout": "single_confirmed_transfer",
    "suspicious_trip_ownership": "no_unauthorized_transfer",
}
COMMON_LEDGER_CHECKS = (
    "provider_baseline_intact",
    "provider_baseline_matches_truth",
    "provider_baselines_unchanged",
    "provider_no_unattributed_transactions",
    "provider_ledger_simulated",
    "provider_correction_fields_valid",
    "provider_correction_within_entitlement",
)


class EvalError(RuntimeError):
    """A required HTTP observation could not be made."""


class _CsrfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        fields = dict(attrs)
        if fields.get("name") == "csrf_token":
            self.value = fields.get("value")


def _details(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("details")
    return value if isinstance(value, dict) else {}


def _events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event_type") == kind]


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _json_response(response: httpx.Response, label: str) -> Any:
    if not response.is_success:
        excerpt = response.text[:200].replace("\n", " ")
        raise EvalError(f"{label}: HTTP {response.status_code}: {excerpt}")
    try:
        return response.json()
    except ValueError as exc:
        raise EvalError(f"{label}: response was not JSON") from exc


def _env_value(name: str, fallback: str = "") -> str:
    """Read process env first, then the local .env used by the Compose demo."""
    direct = os.getenv(name)
    if direct is not None:
        return direct
    env_file = ROOT / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                return value
    return fallback


class HttpEvaluator:
    def __init__(self, args: argparse.Namespace, scenarios: list[Scenario]) -> None:
        self.args = args
        self.scenarios = scenarios
        self.run_id = uuid4().hex[:10]
        self.app = httpx.AsyncClient(
            base_url=args.base_url,
            follow_redirects=True,
            timeout=httpx.Timeout(args.http_timeout, connect=5.0),
        )
        self.provider = httpx.AsyncClient(
            base_url=args.provider_url,
            follow_redirects=True,
            timeout=httpx.Timeout(args.http_timeout, connect=5.0),
            headers={"Authorization": f"Bearer {args.provider_api_key}"},
        )
        self.webhook_latencies_ms: list[float] = []
        self.resolution_latencies_ms: list[float] = []
        self.errors: list[str] = []

    async def close(self) -> None:
        await self.app.aclose()
        await self.provider.aclose()

    async def reset_fixtures(self) -> None:
        if not self.args.test_token:
            raise EvalError("--reset-fixtures requires MOCKPAYSWIFT_TEST_TOKEN")
        response = await self.app.post(
            "/test/reset-fixtures",
            headers={
                "Authorization": f"Bearer {self.args.webhook_token}",
                "X-Test-Token": self.args.test_token,
            },
            timeout=max(120.0, self.args.http_timeout),
        )
        if response.status_code != 204:
            _json_response(response, "fixture reset")
            raise EvalError(f"fixture reset returned unexpected HTTP {response.status_code}")

    async def login_employee(self) -> None:
        response = await self.app.get("/ops/login")
        if not response.is_success:
            raise EvalError(f"employee login page: HTTP {response.status_code}")
        parser = _CsrfParser()
        parser.feed(response.text)
        if not parser.value:
            raise EvalError("employee login page had no CSRF token")
        response = await self.app.post(
            "/ops/login",
            data={
                "employee_id": self.args.employee_id,
                "password": self.args.employee_password,
                "csrf_token": parser.value,
            },
        )
        if not response.is_success or "qd_session" not in self.app.cookies:
            raise EvalError(f"employee login failed: HTTP {response.status_code}")

    async def trace(self, rider_id: str) -> list[dict[str, Any]]:
        payload = _json_response(await self.app.get(f"/trace/{rider_id}"), f"trace for {rider_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise EvalError(f"trace for {rider_id}: missing events list")
        return payload["events"]

    async def pending(self) -> list[dict[str, Any]]:
        payload = _json_response(await self.app.get("/ops/pending"), "ops pending")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise EvalError("ops pending: missing items list")
        return payload["items"]

    async def transactions(self, rider_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await self.provider.get(
                f"/v1/riders/{rider_id}/transactions",
                params={"limit": 1000, "offset": offset},
            )
            payload = _json_response(response, f"provider transactions for {rider_id}")
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise EvalError(f"provider transactions for {rider_id}: missing items list")
            result.extend(payload["items"])
            next_offset = payload.get("next_offset")
            if next_offset is None:
                break
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise EvalError(f"provider transactions for {rider_id}: invalid pagination")
            offset = next_offset
            if len(result) > 10_000:
                raise EvalError("provider transaction pagination exceeded safety limit")
        return result

    async def provider_events(self, payment_id: str) -> list[dict[str, Any]]:
        payload = _json_response(
            await self.provider.get(f"/v1/payments/{payment_id}/events"),
            f"provider events for {payment_id}",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise EvalError(f"provider events for {payment_id}: missing items list")
        return payload["items"]

    async def configure_timeout_for_next_correction(self) -> None:
        if not self.args.test_token:
            raise EvalError("timeout fixture requires MOCKPAYSWIFT_TEST_TOKEN")
        response = await self.provider.post(
            "/test/faults",
            json={"idempotency_prefix": "QD-AUTH-", "mode": "timeout_after_create"},
            headers={"X-Test-Token": self.args.test_token},
        )
        if response.status_code != 204:
            _json_response(response, "configure next-correction timeout")
            raise EvalError(f"timeout fixture returned HTTP {response.status_code}")

    async def send(
        self, scenario: Scenario | CatalogScenario, step: MessageStep, message_id: str
    ) -> tuple[str, float]:
        payload = {
            "message_id": message_id,
            "rider_id": scenario.rider_id,
            "text": step.text,
            "received_at": scenario.received_at,
        }
        started = time.perf_counter()
        response = await self.app.post(
            "/messages",
            json=payload,
            headers={"Authorization": f"Bearer {self.args.webhook_token}"},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        self.webhook_latencies_ms.append(latency_ms)
        body = _json_response(response, f"webhook {message_id}")
        if not isinstance(body, dict) or not isinstance(body.get("reply"), str):
            raise EvalError(f"webhook {message_id}: missing reply string")
        return body["reply"], latency_ms

    async def wait_for_step(
        self, rider_id: str, message_id: str, before_count: int, expected_route: str, started: float
    ) -> tuple[list[dict[str, Any]], float]:
        deadline = time.perf_counter() + self.args.case_timeout
        latest: list[dict[str, Any]] = []
        while time.perf_counter() < deadline:
            latest = await self.trace(rider_id)
            recent = latest[before_count:]
            has_reply = any(
                _details(event).get("message_id") == message_id
                for event in _events(recent, "RIDER_REPLY")
            )
            if has_reply:
                if expected_route != "AUTO":
                    elapsed = (time.perf_counter() - started) * 1000
                    self.resolution_latencies_ms.append(elapsed)
                    return recent, elapsed
                reservations = _events(recent, "PAYMENT_RESERVED")
                if reservations:
                    ids = {_details(event).get("authorization_id") for event in reservations}
                    settled = any(
                        _details(event).get("authorization_id") in ids
                        and _details(event).get("local_status")
                        in {"CONFIRMED", "FAILED", "OPS_REVIEW"}
                        for event in _events(recent, "PAYMENT_RECONCILED")
                    )
                    if settled:
                        elapsed = (time.perf_counter() - started) * 1000
                        self.resolution_latencies_ms.append(elapsed)
                        return recent, elapsed
            await asyncio.sleep(self.args.poll_interval)
        raise EvalError(
            f"{rider_id} {message_id}: processing did not reach the expected observation "
            f"within {self.args.case_timeout:.1f}s"
        )

    async def run_case(self, scenario: Scenario) -> dict[str, Any]:
        case: dict[str, Any] = {
            "rider_id": scenario.rider_id,
            "expected_route": scenario.expected_route,
            "expected_outstanding": str(scenario.expected_outstanding),
            "expected_provider_status": scenario.expected_provider_status,
            "steps": [],
            "checks": {},
            "errors": [],
        }
        try:
            for number, step in enumerate(scenario.messages, 1):
                before = await self.trace(scenario.rider_id)
                message_id = f"eval.{self.run_id}.{scenario.rider_id}.{number}"
                started = time.perf_counter()
                reply, webhook_ms = await self.send(scenario, step, message_id)
                target_route = step.expected_route or (
                    "AUTO"
                    if scenario.expected_route == "AUTO_THEN_NO_PAYMENT"
                    else scenario.expected_route
                )
                events, resolution_ms = await self.wait_for_step(
                    scenario.rider_id,
                    message_id,
                    len(before),
                    target_route,
                    started,
                )
                case["steps"].append(
                    {
                        "message_id": message_id,
                        "message": step.text,
                        "expected_category": step.expected_category,
                        "expected_trip_id": step.expected_trip_id,
                        "expected_route": target_route,
                        "reply": reply,
                        "webhook_latency_ms": round(webhook_ms, 2),
                        "resolution_latency_ms": round(resolution_ms, 2),
                        "events": events,
                    }
                )
            case["trace"] = await self.trace(scenario.rider_id)
            case["transactions"] = await self.transactions(scenario.rider_id)
        except (EvalError, httpx.RequestError) as exc:
            case["errors"].append(str(exc))
        return case


def _step_category(step: dict[str, Any]) -> tuple[str | None, str | None]:
    message_id = step["message_id"]
    events = [
        event
        for event in step["events"]
        if event.get("event_type") in {"DISPUTE_OPENED", "DISPUTE_REVISED"}
        and _details(event).get("message_id") == message_id
    ]
    if not events:
        return None, None
    detail = _details(events[-1])
    return detail.get("category"), detail.get("trip_id")


def _step_calculation(step: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, bool]:
    calculations = _events(step["events"], "FINANCIAL_CALCULATION")
    if not calculations:
        return None, None, False
    detail = _details(calculations[-1])
    return (
        _amount(detail.get("verified_outstanding")),
        _amount(detail.get("issue_available")),
        detail.get("uncertain") is True,
    )


def _step_route(step: dict[str, Any], review_riders: set[str], rider_id: str) -> str:
    if _events(step["events"], "PAYMENT_RESERVED"):
        return "AUTO"
    calculations = _events(step["events"], "FINANCIAL_CALCULATION")
    if calculations:
        detail = _details(calculations[-1])
        if detail.get("case_status") in REVIEW_STATES:
            return "REVIEW"
        if _amount(detail.get("issue_available")) == 0:
            return "NO_PAYMENT"
    if rider_id in review_riders:
        return "REVIEW"
    return "UNOBSERVED"


def _corrections(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in transactions if str(row.get("idempotency_key", "")).startswith("QD-AUTH-")
    ]


def evaluate_case(
    case: dict[str, Any], scenario: Scenario, review_riders: set[str]
) -> dict[str, Any]:
    if case["errors"]:
        return case
    checks: dict[str, bool | None] = {}
    steps = case["steps"]
    tool_events = [event for step in steps for event in _events(step["events"], "AGENT_TOOL")]
    case["tool_calls"] = len(tool_events)
    case["successful_tool_calls"] = sum(
        _details(event).get("success") is True for event in tool_events
    )
    observed_routes: list[str] = []
    for index, (step, expected) in enumerate(zip(steps, scenario.messages, strict=True), 1):
        category, trip_id = _step_category(step)
        step["observed_category"] = category
        step["observed_trip_id"] = trip_id
        checks[f"classification_{index}"] = category == expected.expected_category
        if expected.expected_trip_id:
            checks[f"trip_identification_{index}"] = trip_id == expected.expected_trip_id
        route = _step_route(step, review_riders, scenario.rider_id)
        step["observed_route"] = route
        observed_routes.append(route)
        target = step["expected_route"]
        checks[f"route_{index}"] = route == target
        checks[f"webhook_deadline_{index}"] = step["webhook_latency_ms"] < 10_000
    verified, available, uncertain = _step_calculation(steps[0])
    case["observed_verified_outstanding"] = str(verified) if verified is not None else None
    case["observed_issue_available"] = str(available) if available is not None else None
    expected_available = (
        Decimal(0)
        if uncertain
        or scenario.messages[0].expected_category
        in {"deduction", "delayed_payout", "duplicate_payout"}
        else scenario.expected_outstanding
    )
    checks["financial_calculation"] = (
        verified == scenario.expected_outstanding and available == expected_available
    )

    transactions = case["transactions"]
    baseline = [
        row
        for row in transactions
        if str(row.get("idempotency_key", "")).startswith("base:")
        and row.get("earning_period") == scenario.business_date.isoformat()
    ]
    case["observed_provider_baseline_status"] = (
        baseline[0].get("status") if len(baseline) == 1 else None
    )
    checks["provider_baseline_status"] = (
        len(baseline) == 1 and baseline[0].get("status") == scenario.expected_provider_status
    )
    corrections = _corrections(transactions)
    case["correction_transactions"] = corrections
    correction_total = sum(
        (_amount(row.get("amount")) or Decimal(0) for row in corrections), Decimal(0)
    )
    confirmed = [row for row in corrections if row.get("status") == "CONFIRMED"]
    case["confirmed_correction_amount"] = str(
        sum((_amount(row.get("amount")) or Decimal(0) for row in confirmed), Decimal(0))
    )
    checks["no_overpayment"] = correction_total <= scenario.expected_outstanding
    checks["simulated_provider_only"] = all(row.get("simulated") is True for row in transactions)
    if scenario.expected_route in {"AUTO", "AUTO_THEN_NO_PAYMENT"}:
        checks["automatic_decision"] = (
            len(corrections) == 1
            and len(confirmed) == 1
            and correction_total == scenario.expected_outstanding
        )
    else:
        checks["automatic_decision"] = not corrections
    if scenario.expected_route == "REVIEW":
        checks["human_escalation"] = scenario.rider_id in review_riders
    else:
        checks["human_escalation"] = scenario.rider_id not in review_riders
    if confirmed:
        reconciled = _events(case["trace"], "PAYMENT_RECONCILED")
        checks["reconciliation"] = all(
            any(
                _details(event).get("provider_status") == "CONFIRMED"
                and _details(event).get("local_status") == "CONFIRMED"
                and row.get("idempotency_key")
                == f"QD-AUTH-{_details(event).get('authorization_id')}"
                for event in reconciled
            )
            for row in confirmed
        )
    if scenario.expected_route == "AUTO_THEN_NO_PAYMENT":
        checks["overlap_prevention"] = (
            observed_routes == ["AUTO", "NO_PAYMENT"] and len(corrections) == 1
        )
    case["observed_route"] = "_THEN_".join(observed_routes)
    case["checks"] = checks
    case.pop("trace", None)
    case.pop("transactions", None)
    for step in steps:
        step.pop("events", None)
    return case


async def _send_catalog_step(
    evaluator: HttpEvaluator,
    scenario: CatalogScenario,
    index: int,
    *,
    wait_for_payment: bool,
) -> dict[str, Any]:
    before = await evaluator.trace(scenario.rider_id)
    message_id = f"eval.{evaluator.run_id}.catalog.{scenario.requirement}.{index + 1}"
    started = time.perf_counter()
    ack, webhook_ms = await evaluator.send(
        scenario, MessageStep(scenario.messages[index], ""), message_id
    )
    events, resolution_ms = await evaluator.wait_for_step(
        scenario.rider_id,
        message_id,
        len(before),
        "AUTO" if wait_for_payment else "REVIEW",
        started,
    )
    replies = [
        _details(event).get("reply", "")
        for event in _events(events, "RIDER_REPLY")
        if _details(event).get("message_id") == message_id
    ]
    return {
        "message_id": message_id,
        "text": scenario.messages[index],
        "ack": ack,
        "reply": replies[-1] if replies else "",
        "webhook_latency_ms": round(webhook_ms, 2),
        "resolution_latency_ms": round(resolution_ms, 2),
        "events": events,
    }


def _calculation_for(step: dict[str, Any], dispute_id: str | None = None) -> dict[str, Any]:
    rows = [
        _details(event)
        for event in _events(step["events"], "FINANCIAL_CALCULATION")
        if dispute_id is None or event.get("dispute_id") == dispute_id
    ]
    return rows[-1] if rows else {}


def _opened_by_trip(step: dict[str, Any]) -> dict[str, str]:
    return {
        str(_details(event).get("trip_id")): str(event.get("dispute_id"))
        for event in _events(step["events"], "DISPUTE_OPENED")
    }


def _catalog_checks(
    scenario: CatalogScenario,
    steps: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    pending_riders: set[str],
    *,
    initial_transactions: list[dict[str, Any]] | None = None,
    timeout_events: list[dict[str, Any]] | None = None,
    other_rider_unchanged: bool | None = None,
) -> dict[str, bool]:
    truth = scenario.ground_truth
    corrections = _corrections(transactions)
    correction_total = sum(
        (_amount(row.get("amount")) or Decimal(0) for row in corrections), Decimal(0)
    )
    baseline = [
        row
        for row in transactions
        if str(row.get("idempotency_key", "")).startswith("base:")
        and row.get("earning_period") == scenario.business_date.isoformat()
    ]
    all_baselines = [
        row for row in transactions if str(row.get("idempotency_key", "")).startswith("base:")
    ]
    initial_baselines = [
        row
        for row in (initial_transactions or [])
        if str(row.get("idempotency_key", "")).startswith("base:")
    ]
    checks = {
        "webhook_under_10_seconds": all(step["webhook_latency_ms"] < 10_000 for step in steps),
        "every_message_answered": len(steps) == len(scenario.messages)
        and all(step["reply"] for step in steps),
        "provider_baseline_intact": len(baseline) == 1,
        "provider_baseline_matches_truth": len(baseline) == 1
        and truth.get("baseline_amount") is not None
        and truth.get("baseline_provider_status") is not None
        and _amount(baseline[0].get("amount")) == _amount(truth.get("baseline_amount"))
        and baseline[0].get("status") == truth.get("baseline_provider_status")
        and baseline[0].get("rider_id") == scenario.rider_id
        and baseline[0].get("currency") == "INR",
        "provider_baselines_unchanged": initial_transactions is not None
        and {row.get("payment_id"): row for row in all_baselines}
        == {row.get("payment_id"): row for row in initial_baselines},
        "provider_no_unattributed_transactions": len(transactions)
        == len(all_baselines) + len(corrections),
        "provider_ledger_simulated": all(row.get("simulated") is True for row in transactions),
        "provider_correction_fields_valid": all(
            row.get("rider_id") == scenario.rider_id
            and row.get("currency") == "INR"
            and row.get("status") == "CONFIRMED"
            and bool(row.get("payment_id"))
            and bool(row.get("dispute_reference"))
            and (_amount(row.get("amount")) or Decimal(0)) > 0
            for row in corrections
        )
        and len({row.get("payment_id") for row in corrections}) == len(corrections)
        and len({row.get("idempotency_key") for row in corrections}) == len(corrections),
    }
    cap = next(
        (
            _amount(truth[name])
            for name in (
                "aggregate_outstanding",
                "max_total_correction",
                "verified_outstanding",
                "outstanding",
                "corrected_trip_outstanding",
            )
            if name in truth
        ),
        None,
    )
    checks["provider_correction_within_entitlement"] = cap is not None and correction_total <= cap

    if scenario.requirement == "multiple_disputed_trips":
        opened = _opened_by_trip(steps[0])
        expected = truth["unpaid_by_trip"]
        checks["three_distinct_trip_disputes"] = set(opened) == set(expected)
        first_trip = scenario.source_trip_ids[0]
        checks["first_trip_balance_from_policy"] = _amount(
            _calculation_for(steps[0], opened.get(first_trip)).get("issue_available")
        ) == _amount(expected[first_trip])
        checks["single_auto_correction"] = (
            len(corrections) == 1
            and correction_total == _amount(expected[first_trip])
            and corrections[0].get("status") == "CONFIRMED"
        )
        checks["remaining_trip_cases_reviewed"] = scenario.rider_id in pending_riders
    elif scenario.requirement == "duplicate_complaints":
        checks["same_issue_retained"] = len(
            _events(steps[0]["events"], "DISPUTE_OPENED")
        ) == 1 and not _events(steps[1]["events"], "DISPUTE_OPENED")
        checks["single_verified_transfer"] = (
            len(corrections) == 1
            and correction_total == _amount(truth["outstanding"])
            and corrections[0].get("status") == "CONFIRMED"
        )
        checks["second_message_no_reservation"] = not _events(
            steps[1]["events"], "PAYMENT_RESERVED"
        )
    elif scenario.requirement == "changed_claimed_amount":
        original = _events(steps[0]["events"], "DISPUTE_OPENED")
        revised = _events(steps[1]["events"], "DISPUTE_REVISED")
        checks["claim_revised_in_place"] = (
            len(original) == len(revised) == 1
            and original[0].get("dispute_id") == revised[0].get("dispute_id")
            and _amount(_details(revised[0]).get("claimed_amount"))
            == _amount(truth["final_claimed_amount"])
        )
        checks["verified_balance_not_claim_amount"] = (
            _amount(_calculation_for(steps[0]).get("issue_available"))
            == _amount(truth["verified_outstanding"])
            and _amount(_calculation_for(steps[1]).get("issue_available")) == 0
        )
        checks["settled_transfer_not_rewritten"] = (
            len(corrections) == 1
            and correction_total == _amount(truth["max_total_correction"])
            and corrections[0].get("status") == "CONFIRMED"
            and not _events(steps[1]["events"], "PAYMENT_RESERVED")
        )
    elif scenario.requirement == "changed_trip_identifier":
        original = _events(steps[0]["events"], "DISPUTE_OPENED")
        revised = _events(steps[1]["events"], "DISPUTE_REVISED")
        checks["trip_revised_in_place"] = (
            len(original) == len(revised) == 1
            and original[0].get("dispute_id") == revised[0].get("dispute_id")
            and _details(original[0]).get("trip_id") == scenario.source_trip_ids[0]
            and _details(revised[0]).get("trip_id") == scenario.source_trip_ids[1]
        )
        checks["balance_tracks_corrected_trip"] = _amount(
            _calculation_for(steps[0]).get("issue_available")
        ) == _amount(truth["first_trip_outstanding"]) and _amount(
            _calculation_for(steps[1]).get("issue_available")
        ) == _amount(truth["corrected_trip_outstanding"])
        checks["only_corrected_trip_paid"] = (
            len(corrections) == 1
            and correction_total == _amount(truth["corrected_trip_outstanding"])
            and corrections[0].get("status") == "CONFIRMED"
        )
    elif scenario.requirement == "repeat_daily_claim":
        opened = [_opened_by_trip(step) for step in steps]
        checks["separate_trip_cases"] = all(
            scenario.source_trip_ids[index] in row for index, row in enumerate(opened)
        )
        checks["verified_trip_balances"] = _amount(
            _calculation_for(steps[0]).get("issue_available")
        ) == _amount(truth["first_trip_outstanding"]) and _amount(
            _calculation_for(steps[1]).get("issue_available")
        ) == _amount(truth["second_trip_outstanding"])
        checks["first_auto_second_review"] = (
            len(corrections) == 1
            and correction_total == _amount(truth["first_trip_outstanding"])
            and corrections[0].get("status") == "CONFIRMED"
            and not _events(steps[1]["events"], "PAYMENT_RESERVED")
            and scenario.rider_id in pending_riders
        )
    elif scenario.requirement == "payment_timeout":
        checks["timeout_fault_observed_by_provider"] = any(
            row.get("reason") == "simulated_timeout_after_create" for row in (timeout_events or [])
        )
        checks["single_confirmed_transfer"] = (
            len(corrections) == 1
            and correction_total == _amount(truth["outstanding"])
            and corrections[0].get("status") == truth["eventual_provider_status"]
        )
        checks["same_key_reconciled"] = (
            any(
                _details(event).get("provider_status") == "CONFIRMED"
                and f"QD-AUTH-{_details(event).get('authorization_id')}"
                == corrections[0].get("idempotency_key")
                for event in _events(steps[0]["events"], "PAYMENT_RECONCILED")
            )
            if corrections
            else False
        )
    elif scenario.requirement == "suspicious_trip_ownership":
        opened = _events(steps[0]["events"], "DISPUTE_OPENED")
        checks["foreign_trip_rejected"] = (
            len(opened) == 1
            and _details(opened[0]).get("trip_id") is None
            and "not in your QuickDrop account" in steps[0]["reply"]
        )
        checks["no_foreign_details_disclosed"] = (
            scenario.source_trip_ids[0] not in steps[0]["reply"]
            and "R0002" not in steps[0]["reply"]
            and other_rider_unchanged is True
        )
        checks["no_unauthorized_transfer"] = not corrections and not _events(
            steps[0]["events"], "PAYMENT_RESERVED"
        )
    else:
        raise EvalError(f"no HTTP evaluator for catalog case {scenario.requirement}")
    return checks


def _catalog_ledger_ok(case: dict[str, Any]) -> bool:
    requirement = str(case["requirement"])
    specific = CATALOG_LEDGER_CHECKS.get(requirement)
    if specific is None or case["errors"]:
        return False
    checks = case["checks"]
    return all(checks.get(name) is True for name in (*COMMON_LEDGER_CHECKS, specific))


async def run_catalog_case(evaluator: HttpEvaluator, scenario: CatalogScenario) -> dict[str, Any]:
    result: dict[str, Any] = {
        "requirement": scenario.requirement,
        "rider_id": scenario.rider_id,
        "steps": [],
        "checks": {},
        "errors": [],
    }
    try:
        initial_trace = await evaluator.trace(scenario.rider_id)
        initial_transactions = await evaluator.transactions(scenario.rider_id)
        initial_pending = await evaluator.pending()
        fixture_empty = (
            not initial_trace and not _corrections(initial_transactions) and not initial_pending
        )
        if scenario.requirement == "payment_timeout":
            await evaluator.configure_timeout_for_next_correction()
        other_before = (
            await evaluator.transactions("R0002")
            if scenario.requirement == "suspicious_trip_ownership"
            else None
        )
        auto_steps = {
            "multiple_disputed_trips": {0},
            "duplicate_complaints": {0},
            "changed_claimed_amount": {0},
            "changed_trip_identifier": {1},
            "repeat_daily_claim": {0},
            "payment_timeout": {0},
        }.get(scenario.requirement, set())
        for index in range(len(scenario.messages)):
            result["steps"].append(
                await _send_catalog_step(
                    evaluator, scenario, index, wait_for_payment=index in auto_steps
                )
            )
        transactions = await evaluator.transactions(scenario.rider_id)
        corrections = _corrections(transactions)
        timeout_events = (
            await evaluator.provider_events(corrections[0]["payment_id"])
            if scenario.requirement == "payment_timeout" and len(corrections) == 1
            else None
        )
        pending_riders = {str(row.get("rider_id")) for row in await evaluator.pending()}
        checks = _catalog_checks(
            scenario,
            result["steps"],
            transactions,
            pending_riders,
            initial_transactions=initial_transactions,
            timeout_events=timeout_events,
            other_rider_unchanged=(await evaluator.transactions("R0002") == other_before)
            if other_before is not None
            else None,
        )
        checks["fixture_empty_before_case"] = fixture_empty
        result["checks"] = checks
        result["correction_transactions"] = corrections
        tool_events = [
            event for step in result["steps"] for event in _events(step["events"], "AGENT_TOOL")
        ]
        result["tool_calls"] = len(tool_events)
        result["successful_tool_calls"] = sum(
            _details(event).get("success") is True for event in tool_events
        )
    except (EvalError, httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as exc:
        result["errors"].append(str(exc))
    for step in result["steps"]:
        step.pop("events", None)
    return result


async def _conversation_checks(
    evaluator: HttpEvaluator, scenario: Scenario
) -> dict[str, bool | None]:
    """Exercise a status follow-up and a corrected claimed amount on a paid-in-full case."""
    result: dict[str, bool | None] = {}
    before = await evaluator.trace(scenario.rider_id)
    opened_before = len(_events(before, "DISPUTE_OPENED"))
    status_step = MessageStep("What is the status of my previous complaint?", "")
    message_id = f"eval.{evaluator.run_id}.{scenario.rider_id}.status"
    started = time.perf_counter()
    await evaluator.send(scenario, status_step, message_id)
    events, _ = await evaluator.wait_for_step(
        scenario.rider_id,
        message_id,
        len(before),
        "NO_PAYMENT",
        started,
    )
    result["status_follow_up"] = not _events(events, "DISPUTE_OPENED") and any(
        _details(event).get("message_id") == message_id for event in _events(events, "RIDER_REPLY")
    )
    before = await evaluator.trace(scenario.rider_id)
    correction_step = MessageStep("Actually 120 nahi, Rs 150 kam hai.", "")
    message_id = f"eval.{evaluator.run_id}.{scenario.rider_id}.correction"
    started = time.perf_counter()
    await evaluator.send(scenario, correction_step, message_id)
    events, _ = await evaluator.wait_for_step(
        scenario.rider_id,
        message_id,
        len(before),
        "NO_PAYMENT",
        started,
    )
    revised = [
        event
        for event in _events(events, "DISPUTE_REVISED")
        if _details(event).get("message_id") == message_id
    ]
    latest = await evaluator.trace(scenario.rider_id)
    transactions = await evaluator.transactions(scenario.rider_id)
    result["corrected_claim_preserved"] = (
        len(_events(latest, "DISPUTE_OPENED")) == opened_before
        and any(_amount(_details(event).get("claimed_amount")) == Decimal(150) for event in revised)
        and not _corrections(transactions)
    )
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    scenarios = scenarios_from_manifest(manifest)
    catalog_manifest = json.loads(args.catalog.read_text(encoding="utf-8"))
    catalog_scenarios = catalog_scenarios_from_manifest(catalog_manifest)
    evaluator = HttpEvaluator(args, scenarios)
    cases: list[dict[str, Any]] = []
    catalog_cases: list[dict[str, Any]] = []
    checks: dict[str, list[bool | None]] = {
        "fixture_isolation": [],
        "conversation_state_correctness": [],
        "duplicate_payment_prevention": [],
        "catalog_workflow_correctness": [],
        "catalog_provider_ledger": [],
    }
    metadata = {
        "run_id": evaluator.run_id,
        "base_url": args.base_url,
        "provider_url": args.provider_url,
        "manifest": str(args.manifest),
        "manifest_generator_version": manifest.get("generator_version"),
        "agent_mode": args.agent_mode,
        "fixture_reset_requested": args.reset_fixtures,
        "scenario_count": len(scenarios),
        "catalog": str(args.catalog),
        "catalog_scenario_count": len(catalog_scenarios),
    }
    try:
        await evaluator.login_employee()
        if args.reset_fixtures:
            for catalog_scenario in catalog_scenarios:
                await evaluator.reset_fixtures()
                evaluated = await run_catalog_case(evaluator, catalog_scenario)
                catalog_cases.append(evaluated)
                checks["fixture_isolation"].append(
                    evaluated["checks"].get("fixture_empty_before_case") is True
                )
                checks["catalog_workflow_correctness"].append(
                    bool(evaluated["checks"])
                    and all(evaluated["checks"].values())
                    and not evaluated["errors"]
                )
                checks["catalog_provider_ledger"].append(_catalog_ledger_ok(evaluated))
                evaluator.errors.extend(
                    f"catalog {catalog_scenario.requirement}: {error}"
                    for error in evaluated["errors"]
                )
            await evaluator.reset_fixtures()
            checks["fixture_isolation"].append(
                not await evaluator.trace(catalog_scenarios[-1].rider_id)
                and not _corrections(await evaluator.transactions(catalog_scenarios[-1].rider_id))
            )
        else:
            checks["fixture_isolation"] = [None] * (len(catalog_scenarios) + 1)
            checks["catalog_workflow_correctness"] = [None] * len(catalog_scenarios)
            checks["catalog_provider_ledger"] = [None] * len(catalog_scenarios)
        for scenario in scenarios:
            cases.append(await evaluator.run_case(scenario))
        pending = await evaluator.pending()
        review_riders = {str(row.get("rider_id")) for row in pending}
        for case, scenario in zip(cases, scenarios, strict=True):
            evaluated = evaluate_case(case, scenario, review_riders)
            for name, value in evaluated["checks"].items():
                if name.startswith("classification_"):
                    metric = "complaint_classification_accuracy"
                elif name.startswith("trip_identification_"):
                    metric = "relevant_trip_identification_accuracy"
                elif name.startswith("route_"):
                    metric = "policy_route_accuracy"
                elif name.startswith("webhook_deadline_"):
                    metric = "webhook_under_10_seconds"
                else:
                    metric = name
                checks.setdefault(metric, []).append(value)
            if evaluated["errors"]:
                evaluator.errors.extend(
                    f"{scenario.rider_id}: {error}" for error in evaluated["errors"]
                )

        by_rider = {scenario.rider_id: scenario for scenario in scenarios}
        if (
            "R0003" in by_rider
            and not next(case for case in cases if case["rider_id"] == "R0003")["errors"]
        ):
            try:
                conversation = await _conversation_checks(evaluator, by_rider["R0003"])
                checks["conversation_state_correctness"].extend(conversation.values())
                metadata["conversation_checks"] = conversation
            except (EvalError, httpx.RequestError) as exc:
                evaluator.errors.append(f"conversation follow-up: {exc}")
                checks["conversation_state_correctness"].extend([None, None])

        if (
            "R0001" in by_rider
            and not next(case for case in cases if case["rider_id"] == "R0001")["errors"]
        ):
            try:
                first = next(case for case in cases if case["rider_id"] == "R0001")
                step = by_rider["R0001"].messages[0]
                original_id = first["steps"][0]["message_id"]
                trace_before = await evaluator.trace("R0001")
                transactions_before = await evaluator.transactions("R0001")
                await evaluator.send(by_rider["R0001"], step, original_id)
                trace_after = await evaluator.trace("R0001")
                transactions_after = await evaluator.transactions("R0001")
                duplicate_safe = len(_events(trace_before, "MESSAGE_RECEIVED")) == len(
                    _events(trace_after, "MESSAGE_RECEIVED")
                ) and len(_corrections(transactions_before)) == len(
                    _corrections(transactions_after)
                )
                checks["duplicate_payment_prevention"].append(duplicate_safe)
            except (EvalError, httpx.RequestError) as exc:
                evaluator.errors.append(f"duplicate message check: {exc}")
                checks["duplicate_payment_prevention"].append(None)
        overlap = next((case for case in cases if case["rider_id"] == "R0012"), None)
        if overlap and "overlap_prevention" in overlap["checks"]:
            checks["conversation_state_correctness"].append(overlap["checks"]["overlap_prevention"])
    except (EvalError, httpx.RequestError) as exc:
        evaluator.errors.append(str(exc))
    finally:
        await evaluator.close()

    all_cases = [*cases, *catalog_cases]
    tool_count = sum(case.get("tool_calls", 0) for case in all_cases)
    tool_successes = sum(case.get("successful_tool_calls", 0) for case in all_cases)
    checks["agent_tools_exercised"] = [tool_count > 0]
    metadata["fixture_reset_completed"] = bool(checks["fixture_isolation"]) and all(
        value is True for value in checks["fixture_isolation"]
    )
    report = build_report(
        cases=cases,
        checks=checks,
        webhook_latencies_ms=evaluator.webhook_latencies_ms,
        resolution_latencies_ms=evaluator.resolution_latencies_ms,
        tool_calls=tool_count,
        successful_tool_calls=tool_successes,
        metadata=metadata,
        errors=evaluator.errors,
    )
    report["catalog_cases"] = catalog_cases
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--provider-url", default=_env_value("MOCKPAYSWIFT_URL", "http://localhost:8001")
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--reset-fixtures",
        action="store_true",
        help="call protected test-only reset and reseed before evaluating",
    )
    parser.add_argument("--webhook-token", default=_env_value("WEBHOOK_TOKEN"))
    parser.add_argument("--provider-api-key", default=_env_value("MOCKPAYSWIFT_API_KEY"))
    parser.add_argument("--test-token", default=_env_value("MOCKPAYSWIFT_TEST_TOKEN"))
    parser.add_argument("--employee-id", default=_env_value("DEMO_EMPLOYEE_ID", "OPS001"))
    parser.add_argument("--employee-password", default=_env_value("DEMO_EMPLOYEE_PASSWORD"))
    parser.add_argument("--agent-mode", default=_env_value("AGENT_MODE", "unknown"))
    parser.add_argument("--case-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not args.manifest.is_file():
        parser.error(f"golden manifest does not exist: {args.manifest}")
    if not args.catalog.is_file():
        parser.error(f"dispute catalog does not exist: {args.catalog}")
    for name in ("webhook_token", "provider_api_key", "employee_password"):
        if not getattr(args, name):
            parser.error(f"{name.replace('_', '-')} is required (option or environment)")
    if args.case_timeout <= 0 or args.poll_interval <= 0 or args.http_timeout <= 0:
        parser.error("timeouts and poll interval must be positive")
    return args


def main() -> None:
    args = _parse_args()
    try:
        report = asyncio.run(run(args))
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"Evaluation setup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"QuickDrop evaluation: {report['status']} — "
        f"{len(report['cases'])} golden + {len(report['catalog_cases'])} catalog cases"
    )
    for name, result in report["metrics"].items():
        if "evaluated" in result:
            print(
                f"  {name}: {result['passed']}/{result['evaluated']}"
                f" ({result['unavailable']} unavailable)"
            )
        else:
            print(
                f"  {name}: p50={result['p50_ms']}ms p95={result['p95_ms']}ms"
                f" ({result['samples']} samples)"
            )
    print(f"JSON report: {args.output}")
    if report["errors"]:
        for error in report["errors"]:
            print(f"  ERROR: {error}", file=sys.stderr)
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
