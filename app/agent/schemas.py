"""Validated data crossing the model / application boundary."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IssueCategory(StrEnum):
    SURGE = "surge"
    BASE_PAY = "base_pay"
    INCENTIVE = "incentive"
    DEDUCTION = "deduction"
    PARTIAL_PAYOUT = "partial_payout"
    DELAYED_PAYOUT = "delayed_payout"
    DUPLICATE_PAYOUT = "duplicate_payout"
    TOTAL_PAYOUT = "total_payout"
    OTHER = "other"


class ExtractedIssue(StrictModel):
    category: IssueCategory
    trip_id: str | None = Field(default=None, max_length=40)
    earning_date: date | None = None
    claimed_amount: Decimal | None = Field(default=None, ge=Decimal(0))
    correction: bool = False
    target_issue_id: str | None = Field(default=None, max_length=80)
    confidence: float = Field(default=1.0, ge=0, le=1)
    clarification_needed: bool = False

    @field_validator("trip_id")
    @classmethod
    def normalize_trip_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if not value or not value.replace("-", "").isalnum():
            raise ValueError("invalid trip identifier")
        return value

    @field_validator("claimed_amount")
    @classmethod
    def exact_minor_units(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value != value.quantize(Decimal("0.01")):
            raise ValueError("claimed amount requires at most two decimal places")
        return value


class ComplaintExtraction(StrictModel):
    language: str = Field(default="unknown", max_length=24)
    issues: list[ExtractedIssue] = Field(default_factory=list, max_length=8)
    status_request: bool = False
    clarification_question: str | None = Field(default=None, max_length=400)


class IssueRevision(StrictModel):
    message_id: str
    previous: dict[str, Any]
    current: dict[str, Any]
    reason: str


class IssueState(StrictModel):
    issue_id: str
    category: IssueCategory
    trip_id: str | None = None
    earning_date: date | None = None
    original_claimed_amount: Decimal | None = None
    claimed_amount: Decimal | None = None
    status: str = "open"
    source_message_ids: list[str] = Field(default_factory=list)
    revisions: list[IssueRevision] = Field(default_factory=list)
    requires_review: bool = False


class ConversationState(StrictModel):
    rider_id: str
    conversation_id: str
    issues: list[IssueState] = Field(default_factory=list)
    processed_message_ids: list[str] = Field(default_factory=list)
    previous_reply_by_message: dict[str, str] = Field(default_factory=dict)


class ToolCall(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(StrictModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    success: bool


class AgentResult(StrictModel):
    reply: str
    extraction: ComplaintExtraction
    issues: list[IssueState]
    tool_trace: list[ToolResult] = Field(default_factory=list)
    requires_review: bool = False
    clarification_needed: bool = False
    duplicate: bool = False
    deferred: bool = False
    processed_at: datetime | None = None
