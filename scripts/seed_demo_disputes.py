"""Create or reuse three safe portfolio disputes through public QuickDrop HTTP APIs.

Run this only against synthetic source data and MockPaySwift. The script does not
reset fixtures, approve a case, or call a payment submission endpoint. Existing
matching cases are reused, so it is safe to rerun after an offline evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

import httpx

from app.config import get_settings


class DemoError(RuntimeError):
    """The target deployment cannot safely provide the requested demo cases."""


class _CsrfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            values = dict(attrs)
            if values.get("name") == "csrf_token":
                self.value = values.get("value")


@dataclass(frozen=True)
class DemoCase:
    name: str
    rider_id: str
    message_id: str
    message: str
    category: str
    route: str
    baseline: Decimal
    outstanding: Decimal


BUSINESS_DATE = "2026-10-17"
RECEIVED_AT = "2026-10-18T09:05:00+05:30"
CASES = (
    DemoCase(
        "completed correction",
        "R0002",
        "demo.v1.R0002.incentive",
        "2026-10-17 ka incentive missing hai.",
        "incentive",
        "PAID",
        Decimal("886.64"),
        Decimal("50.00"),
    ),
    DemoCase(
        "awaiting employee approval",
        "R0011",
        "demo.v1.R0011.review",
        "My payout for 2026-10-17 is Rs 200.01 short.",
        "total_payout",
        "PENDING_APPROVAL",
        Decimal("930.31"),
        Decimal("200.01"),
    ),
    DemoCase(
        "no unpaid balance",
        "R0006",
        "demo.v1.R0006.deduction",
        "The deduction on my 2026-10-17 payout looks wrong.",
        "deduction",
        "NO_BALANCE",
        Decimal("486.28"),
        Decimal("0.00"),
    ),
)


def _details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details")
    return details if isinstance(details, dict) else {}


def _case_dispute_ids(case: DemoCase, events: list[dict[str, Any]]) -> set[str]:
    return {
        str(event["dispute_id"])
        for event in events
        if event.get("event_type") in {"DISPUTE_OPENED", "DISPUTE_REVISED"}
        and event.get("dispute_id")
        and _details(event).get("category") == case.category
        and _details(event).get("earning_date") == BUSINESS_DATE
    }


def _corrections(case: DemoCase, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require the expected provider baseline and bound every correction."""
    if any(row.get("rider_id") != case.rider_id for row in rows):
        raise DemoError(f"{case.rider_id}: provider returned another rider's transaction")
    baseline_key = f"base:EP:{case.rider_id}:{BUSINESS_DATE}"
    baseline = [row for row in rows if row.get("idempotency_key") == baseline_key]
    if len(baseline) != 1:
        raise DemoError(f"{case.rider_id}: expected one source-period provider baseline")
    if (
        baseline[0].get("status") != "CONFIRMED"
        or Decimal(str(baseline[0].get("amount"))) != case.baseline
    ):
        raise DemoError(f"{case.rider_id}: provider baseline differs from golden source data")
    if any(row.get("simulated") is not True for row in rows):
        raise DemoError(f"{case.rider_id}: provider returned a non-simulated transaction")
    corrections = []
    for row in rows:
        key = str(row.get("idempotency_key", ""))
        if key.startswith("base:EP:"):
            continue
        if not key.startswith("QD-AUTH-"):
            raise DemoError(f"{case.rider_id}: unexpected provider transaction")
        corrections.append(row)
    if case.route != "PAID" and corrections:
        raise DemoError(f"{case.rider_id}: this demo case must not have a correction")
    if case.route == "PAID":
        if len(corrections) > 1:
            raise DemoError(f"{case.rider_id}: more than one correction exists")
        if corrections and (
            corrections[0].get("status") != "CONFIRMED"
            or Decimal(str(corrections[0].get("amount"))) != case.outstanding
        ):
            raise DemoError(f"{case.rider_id}: correction differs from verified entitlement")
    return corrections


def _is_ready(
    case: DemoCase,
    events: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> bool:
    corrections = _corrections(case, transactions)
    dispute_ids = _case_dispute_ids(case, events)
    if not dispute_ids:
        return False
    calculations = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("event_type") == "FINANCIAL_CALCULATION"
        and event.get("dispute_id") in dispute_ids
    ]
    if not calculations:
        return False
    last_calculation_index, last_calculation = calculations[-1]
    if (
        Decimal(str(_details(last_calculation).get("verified_outstanding", "-1")))
        != case.outstanding
    ):
        return False
    if case.route == "PAID":
        return bool(
            len(corrections) == 1
            and corrections[0].get("dispute_reference") in dispute_ids
            and any(
                index > last_calculation_index
                and event.get("event_type") == "PAYMENT_RECONCILED"
                and event.get("dispute_id") == corrections[0].get("dispute_reference")
                and _details(event).get("local_status") == "CONFIRMED"
                for index, event in enumerate(events)
            )
        )
    if case.route == "PENDING_APPROVAL":
        return _details(last_calculation).get("case_status") == "PENDING_APPROVAL" and any(
            row.get("id") in dispute_ids
            and row.get("rider_id") == case.rider_id
            and row.get("status") == "PENDING_APPROVAL"
            and Decimal(str(row.get("verified_outstanding", "-1"))) == case.outstanding
            for row in pending
        )
    return _details(last_calculation).get("case_status") == "NO_BALANCE"


async def _trace(app: httpx.AsyncClient, rider_id: str) -> list[dict[str, Any]]:
    response = await app.get(f"/trace/{rider_id}")
    response.raise_for_status()
    events = response.json().get("events")
    if not isinstance(events, list):
        raise DemoError(f"{rider_id}: trace response has no events list")
    return events


async def _transactions(provider: httpx.AsyncClient, rider_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = await provider.get(
            f"/v1/riders/{rider_id}/transactions", params={"limit": 1000, "offset": offset}
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("items")
        if not isinstance(page, list):
            raise DemoError(f"{rider_id}: provider transaction response has no items list")
        rows.extend(page)
        next_offset = payload.get("next_offset")
        if next_offset is None:
            return rows
        if not isinstance(next_offset, int) or next_offset <= offset or len(rows) > 10_000:
            raise DemoError(f"{rider_id}: invalid provider pagination")
        offset = next_offset


async def _pending(app: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        response = await app.get("/ops/pending", params={"page": page})
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items")
        if not isinstance(items, list):
            raise DemoError("operations pending response has no items list")
        rows.extend(items)
        next_page = payload.get("next_page")
        if next_page is None:
            return rows
        if not isinstance(next_page, int) or next_page <= page or page > 100:
            raise DemoError("invalid operations queue pagination")
        page = next_page


async def _login(app: httpx.AsyncClient, employee_id: str, password: str) -> None:
    page = await app.get("/ops/login")
    page.raise_for_status()
    csrf = _CsrfParser()
    csrf.feed(page.text)
    if not csrf.value:
        raise DemoError("operations login form has no CSRF token")
    response = await app.post(
        "/ops/login",
        data={"employee_id": employee_id, "password": password, "csrf_token": csrf.value},
    )
    if response.status_code != 303 or "qd_session" not in app.cookies:
        raise DemoError(f"operations login failed: HTTP {response.status_code}")


async def seed_demo(
    base_url: str,
    provider_url: str,
    *,
    employee_id: str,
    employee_password: str,
    webhook_token: str,
    provider_api_key: str,
    timeout: float,
    poll_interval: float,
) -> list[tuple[str, str]]:
    if not all((employee_password, webhook_token, provider_api_key)):
        raise DemoError("employee password, webhook token, and provider API key are required")
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=15, follow_redirects=False) as app,
        httpx.AsyncClient(
            base_url=provider_url,
            headers={"Authorization": f"Bearer {provider_api_key}"},
            timeout=15,
        ) as provider,
    ):
        await _login(app, employee_id, employee_password)
        pending = await _pending(app)
        actions: list[tuple[DemoCase, str]] = []
        # Inspect all three riders before sending any message. A prior evaluation
        # can already supply the examples; unfamiliar conversations are left alone.
        for case in CASES:
            events = await _trace(app, case.rider_id)
            transactions = await _transactions(provider, case.rider_id)
            if _is_ready(case, events, transactions, pending):
                actions.append((case, "reused"))
                continue
            received = {
                _details(event).get("message_id")
                for event in events
                if event.get("event_type") == "MESSAGE_RECEIVED"
            }
            if received - {case.message_id}:
                raise DemoError(
                    f"{case.rider_id}: existing conversation has a different message; "
                    "leave it intact and use a fresh source-seeded deployment"
                )
            actions.append((case, "send"))

        for case, action in actions:
            if action == "send":
                response = await app.post(
                    "/messages",
                    headers={"Authorization": f"Bearer {webhook_token}"},
                    json={
                        "message_id": case.message_id,
                        "rider_id": case.rider_id,
                        "text": case.message,
                        "received_at": RECEIVED_AT,
                    },
                )
                response.raise_for_status()
                if not isinstance(response.json().get("reply"), str):
                    raise DemoError(f"{case.rider_id}: webhook response has no reply")

        deadline = time.monotonic() + timeout
        results: list[tuple[str, str]] = []
        for case, action in actions:
            while True:
                events = await _trace(app, case.rider_id)
                transactions = await _transactions(provider, case.rider_id)
                pending = await _pending(app)
                if _is_ready(case, events, transactions, pending):
                    results.append((case.name, action))
                    break
                if time.monotonic() >= deadline:
                    raise DemoError(
                        f"{case.rider_id}: {case.route} was not observed within {timeout}s"
                    )
                await asyncio.sleep(poll_interval)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--provider-url", default=None)
    parser.add_argument("--employee-id", default="OPS001")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll_interval <= 0:
        parser.error("timeout and poll interval must be positive")
    settings = get_settings()
    try:
        results = asyncio.run(
            seed_demo(
                args.base_url,
                args.provider_url or settings.mockpayswift_url,
                employee_id=args.employee_id,
                employee_password=settings.demo_employee_password,
                webhook_token=settings.webhook_token,
                provider_api_key=settings.mockpayswift_api_key,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
            )
        )
    except (DemoError, httpx.HTTPError) as exc:
        parser.exit(1, f"Demo dispute fixture failed: {exc}\n")
    for name, action in results:
        print(f"{name}: {action}")


if __name__ == "__main__":
    main()
