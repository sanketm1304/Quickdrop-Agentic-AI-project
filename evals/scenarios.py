"""Messages tied to the independent golden fixture manifest.

The expected money, provider state and route come from scripts/golden_manifest.json.
Only the rider wording and its intended complaint category live here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class MessageStep:
    text: str
    expected_category: str
    expected_trip_id: str | None = None
    expected_route: str | None = None


@dataclass(frozen=True)
class Scenario:
    rider_id: str
    business_date: date
    expected_route: str
    expected_outstanding: Decimal
    expected_provider_status: str
    messages: tuple[MessageStep, ...]

    @property
    def received_at(self) -> str:
        # Keep relative-date interpretation stable across machines and test runs.
        return _received_at(self.business_date)


def _received_at(business_date: date) -> str:
    at = datetime.combine(
        business_date + timedelta(days=1),
        time(9, 5),
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    return at.isoformat()


@dataclass(frozen=True)
class CatalogScenario:
    requirement: str
    rider_id: str
    business_date: date
    source_trip_ids: tuple[str, ...]
    messages: tuple[str, ...]
    ground_truth: dict[str, Any]

    @property
    def received_at(self) -> str:
        return _received_at(self.business_date)


def catalog_scenarios_from_manifest(catalog: dict[str, Any]) -> list[CatalogScenario]:
    """Catalog entries without a golden case must still execute over HTTP."""
    rows = catalog.get("scenarios")
    if not isinstance(rows, list) or not rows:
        raise ValueError("dispute catalog must contain a nonempty scenarios list")
    business_date = date.fromisoformat(str(catalog["source_business_date"]))
    scenarios: list[CatalogScenario] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("dispute catalog case must be an object")
        requirement = str(row["requirement"])
        if requirement in seen:
            raise ValueError(f"duplicate dispute catalog requirement {requirement}")
        seen.add(requirement)
        if row.get("golden_case") is not None:
            continue
        messages = row.get("message_sequence")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(isinstance(message, str) and message.strip() for message in messages)
        ):
            raise ValueError(f"catalog-only case {requirement} needs a message sequence")
        truth = row.get("ground_truth")
        if not isinstance(truth, dict):
            raise TypeError(f"catalog-only case {requirement} needs ground truth")
        scenarios.append(
            CatalogScenario(
                requirement=requirement,
                rider_id=str(row["source_rider_id"]),
                business_date=business_date,
                source_trip_ids=tuple(str(value) for value in row["source_trip_ids"]),
                messages=tuple(messages),
                ground_truth=truth,
            )
        )
    if not scenarios:
        raise ValueError("dispute catalog has no catalog-only HTTP cases")
    return scenarios


def _message_steps(row: dict[str, Any]) -> tuple[MessageStep, ...]:
    rider_id = row["rider_id"]
    day = row["business_date"]
    trip = row["trip_id"]
    amount = row["verified_outstanding"]
    if rider_id == "R0001":
        return (MessageStep(f"Bhai order {trip} ka surge nahi mila.", "surge", trip),)
    if rider_id == "R0002":
        return (MessageStep(f"{day} ka incentive missing hai.", "incentive"),)
    if rider_id == "R0003":
        return (
            MessageStep(
                f"Please check base pay for trip {trip}; it looks missing.", "base_pay", trip
            ),
        )
    if rider_id == "R0004":
        return (MessageStep(f"My total payout for {day} is short.", "total_payout"),)
    if rider_id == "R0005":
        return (MessageStep(f"My partial payout for {day} is {amount} short.", "partial_payout"),)
    if rider_id == "R0006":
        return (MessageStep(f"The deduction on my {day} payout looks wrong.", "deduction"),)
    if rider_id == "R0007":
        return (MessageStep(f"My payout for {day} is delayed.", "delayed_payout"),)
    if rider_id == "R0008":
        return (
            MessageStep(f"My payout for {day} shows reversed; please check it.", "total_payout"),
        )
    if rider_id == "R0009":
        return (MessageStep(f"My payout for {day} failed; please check it.", "total_payout"),)
    if rider_id in {"R0010", "R0011"}:
        return (MessageStep(f"My payout for {day} is Rs {amount} short.", "total_payout"),)
    if rider_id == "R0012":
        return (
            MessageStep(f"Order {trip} ka surge missing hai.", "surge", trip, "AUTO"),
            MessageStep(
                f"My total payout for {day} is short.", "total_payout", expected_route="NO_PAYMENT"
            ),
        )
    raise ValueError(f"no evaluation wording defined for golden rider {rider_id}")


def scenarios_from_manifest(manifest: dict[str, Any]) -> list[Scenario]:
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden manifest must contain a nonempty cases list")
    result: list[Scenario] = []
    seen: set[str] = set()
    for row in cases:
        if not isinstance(row, dict):
            raise TypeError("golden case must be an object")
        for key in (
            "rider_id",
            "business_date",
            "trip_id",
            "expected_route",
            "verified_outstanding",
            "provider_status",
        ):
            if key not in row:
                raise ValueError(f"golden case missing {key}")
        rider_id = str(row["rider_id"])
        if rider_id in seen:
            raise ValueError(f"duplicate golden rider {rider_id}")
        seen.add(rider_id)
        route = str(row["expected_route"])
        if route not in {"AUTO", "REVIEW", "NO_PAYMENT", "AUTO_THEN_NO_PAYMENT"}:
            raise ValueError(f"unsupported expected route {route}")
        result.append(
            Scenario(
                rider_id=rider_id,
                business_date=date.fromisoformat(str(row["business_date"])),
                expected_route=route,
                expected_outstanding=Decimal(str(row["verified_outstanding"])),
                expected_provider_status=str(row["provider_status"]),
                messages=_message_steps(row),
            )
        )
    return result
