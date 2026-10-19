"""External API schemas; all amounts are decimal strings in INR."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PaymentStatus = Literal["CREATED", "PENDING", "CONFIRMED", "FAILED", "REVERSED", "UNKNOWN"]
TestFaultMode = Literal[
    "fail_before_create",
    "timeout_after_create",
    "delayed",
    "failed_settlement",
    "unknown",
    "unavailable",
    "unexpected_response",
]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    rider_id: str = Field(min_length=1, max_length=128)
    dispute_reference: str = Field(min_length=1, max_length=128)
    amount: str
    currency: Literal["INR"] = "INR"
    source_ref: str | None = Field(default=None, max_length=128)
    earning_period: str | None = Field(default=None, max_length=128)

    @field_validator("rider_id", "dispute_reference", "source_ref", "earning_period")
    @classmethod
    def safe_reference(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(ch) < 32 for ch in value)):
            raise ValueError("reference must contain printable nonblank characters")
        return value

    @field_validator("amount")
    @classmethod
    def valid_amount(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", value):
            raise ValueError("amount must be a positive decimal string with at most two places")
        amount = Decimal(value)
        if amount <= 0:
            raise ValueError("amount must be positive")
        return f"{amount:.2f}"


class PaymentView(BaseModel):
    payment_id: str
    idempotency_key: str
    rider_id: str
    dispute_reference: str
    amount: str
    currency: Literal["INR"]
    status: PaymentStatus
    source_ref: str | None
    earning_period: str | None
    created_at: str
    updated_at: str
    settled_at: str | None
    simulated: Literal[True] = True


class FaultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    idempotency_prefix: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    mode: TestFaultMode
    delay_ms: int = Field(default=0, ge=0, le=86_400_000)

    @model_validator(mode="after")
    def exactly_one_target(self) -> FaultCreate:
        if (self.idempotency_key is None) == (self.idempotency_prefix is None):
            raise ValueError("supply exactly one idempotency key or prefix")
        return self


class ResolveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PENDING", "CONFIRMED", "FAILED", "UNKNOWN"]


class SeedPayment(PaymentCreate):
    status: Literal["CREATED", "CONFIRMED", "FAILED", "PENDING", "UNKNOWN", "REVERSED"] = (
        "CONFIRMED"
    )


class SeedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payments: list[SeedPayment] = Field(min_length=1, max_length=1000)


class OutageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unavailable: bool
