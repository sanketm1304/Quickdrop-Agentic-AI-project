"""Explicit allowlist and typed arguments for read-only investigation tools."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from .schemas import IssueCategory, StrictModel


class TripsArgs(StrictModel):
    earning_date: date | None = None
    limit: int = Field(default=25, ge=1, le=50)


class TripDetailsArgs(StrictModel):
    trip_id: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9-]+$")


class PayoutHistoryArgs(StrictModel):
    earning_date: date | None = None
    limit: int = Field(default=25, ge=1, le=50)


class ProviderTransactionsArgs(StrictModel):
    earning_date: date | None = None
    limit: int = Field(default=25, ge=1, le=50)


class PolicyArgs(StrictModel):
    earning_date: date | None = None


class OutstandingArgs(StrictModel):
    category: IssueCategory
    trip_id: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    earning_date: date | None = None


class DisputeHistoryArgs(StrictModel):
    limit: int = Field(default=25, ge=1, le=50)


TOOL_SCHEMAS: dict[str, type[StrictModel]] = {
    "get_rider_trips": TripsArgs,
    "get_trip_details": TripDetailsArgs,
    "get_payout_history": PayoutHistoryArgs,
    "get_provider_transactions": ProviderTransactionsArgs,
    "get_applicable_policy": PolicyArgs,
    "calculate_verified_outstanding": OutstandingArgs,
    "get_dispute_history": DisputeHistoryArgs,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_rider_trips": "List authenticated rider's trips for a business date.",
    "get_trip_details": "Get one trip only if it belongs to the authenticated rider.",
    "get_payout_history": "List authenticated rider's payout records.",
    "get_provider_transactions": "Read independent provider transactions for authenticated rider.",
    "get_applicable_policy": "Read the versioned payout policy for an earning date.",
    "calculate_verified_outstanding": "Read a deterministic calculation of verified outstanding earnings; does not authorize payment.",
    "get_dispute_history": "List authenticated rider's previous dispute records.",
}


def gemini_tool_declarations() -> list[object]:
    """Build manual function declarations with no write-capable methods."""
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            parameters_json_schema=schema.model_json_schema(),
        )
        for name, schema in TOOL_SCHEMAS.items()
    ]
    return [types.Tool(function_declarations=declarations)]
