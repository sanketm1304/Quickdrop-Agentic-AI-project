"""Provider-backed earnings verification and component-level reservation."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.mockpayswift_client import (
    MockPaySwiftClient,
    ProviderPayment,
    ProviderProtocolError,
    ProviderUnavailable,
)
from app.models import (
    Deduction,
    EarningAdjustmentAllocation,
    EarningComponent,
    EarningPeriod,
    PaymentAuthorization,
    PayoutAllocation,
    PayoutRecord,
)
from app.policy import available_to_authorize, money


@dataclass(frozen=True)
class Verification:
    period_id: str
    expected: Decimal
    confirmed_paid: Decimal
    reserved: Decimal
    verified_outstanding: Decimal
    available: Decimal
    component_available: dict[str, Decimal]
    uncertain: bool
    reason: str | None
    evidence: list[dict]


def _validate_payment(
    payment: ProviderPayment | None,
    *,
    key: str,
    rider_id: str,
    reference: str,
    amount: Decimal,
) -> str:
    if payment is None:
        return "MISSING"
    if (
        payment.idempotency_key != key
        or payment.rider_id != rider_id
        or payment.dispute_reference != reference
        or payment.amount != money(amount)
    ):
        return "MISMATCH"
    return payment.status


async def verify_period(
    session: AsyncSession,
    period_id: str,
    provider: MockPaySwiftClient,
) -> Verification:
    period = await session.get(EarningPeriod, period_id)
    if period is None:
        raise LookupError("earning period not found")
    components = list(
        (
            await session.scalars(
                select(EarningComponent).where(EarningComponent.period_id == period_id)
            )
        ).all()
    )
    deductions = list(
        (await session.scalars(select(Deduction).where(Deduction.period_id == period_id))).all()
    )
    payouts = list(
        (
            await session.scalars(select(PayoutRecord).where(PayoutRecord.period_id == period_id))
        ).all()
    )
    auth_rows = (
        await session.execute(
            select(PaymentAuthorization, EarningAdjustmentAllocation)
            .join(
                EarningAdjustmentAllocation,
                EarningAdjustmentAllocation.authorization_id == PaymentAuthorization.id,
            )
            .join(EarningComponent, EarningComponent.id == EarningAdjustmentAllocation.component_id)
            .where(EarningComponent.period_id == period_id)
        )
    ).all()

    expected = money(
        sum((c.amount for c in components), Decimal(0))
        - sum((d.amount for d in deductions), Decimal(0))
    )
    if expected != money(period.expected_amount):
        return Verification(
            period_id,
            expected,
            money(0),
            money(0),
            money(0),
            money(0),
            {},
            True,
            "stored expected amount does not match source components",
            [],
        )
    confirmed_by_component: dict[str, Decimal] = defaultdict(lambda: money(0))
    reserved_by_component: dict[str, Decimal] = defaultdict(lambda: money(0))
    confirmed_paid = money(0)
    reserved = money(0)
    uncertain = False
    reasons: list[str] = []
    evidence: list[dict] = []
    for payout in payouts:
        try:
            payment = await provider.by_key(payout.idempotency_key)
            status = _validate_payment(
                payment,
                key=payout.idempotency_key,
                rider_id=period.rider_id,
                reference=payout.id,
                amount=payout.amount,
            )
        except (ProviderUnavailable, ProviderProtocolError):
            status = "UNAVAILABLE"
        allocations = list(
            (
                await session.scalars(
                    select(PayoutAllocation).where(PayoutAllocation.payout_record_id == payout.id)
                )
            ).all()
        )
        allocated = money(sum((a.amount for a in allocations), Decimal(0)))
        if allocated != money(payout.amount):
            status = "MISMATCH"
        evidence.append(
            {
                "kind": "BASE_PAYOUT",
                "reference": payout.id,
                "provider_status": status,
                "amount": str(payout.amount),
            }
        )
        if status == "CONFIRMED":
            confirmed_paid += money(payout.amount)
            for allocation in allocations:
                confirmed_by_component[allocation.component_id] += money(allocation.amount)
        else:
            uncertain = True
            reasons.append(f"baseline payout {payout.id} is {status}")
    if not payouts:
        uncertain = True
        reasons.append("no provider-verifiable baseline payout")

    seen_auths: dict[str, str] = {}
    for auth, allocation in auth_rows:
        if auth.id not in seen_auths:
            try:
                payment = await provider.by_key(auth.idempotency_key)
                status = _validate_payment(
                    payment,
                    key=auth.idempotency_key,
                    rider_id=period.rider_id,
                    reference=auth.dispute_id,
                    amount=auth.amount,
                )
            except (ProviderUnavailable, ProviderProtocolError):
                status = "UNAVAILABLE"
            seen_auths[auth.id] = status
            evidence.append(
                {
                    "kind": "ADJUSTMENT",
                    "reference": auth.id,
                    "provider_status": status,
                    "amount": str(auth.amount),
                }
            )
            if status not in {"CONFIRMED", "FAILED"} and not (
                auth.status == "CANCELLED" and status == "MISSING"
            ):
                uncertain = True
                reasons.append(f"correction {auth.id} is {status}")
        status = seen_auths[auth.id]
        if status == "CONFIRMED":
            confirmed_by_component[allocation.component_id] += money(allocation.amount)
        elif status not in {"FAILED"} and auth.status != "CANCELLED":
            reserved_by_component[allocation.component_id] += money(allocation.amount)
    confirmed_paid += money(
        sum(
            (
                auth.amount
                for auth_id, status in seen_auths.items()
                if status == "CONFIRMED"
                for auth in [next(a for a, _ in auth_rows if a.id == auth_id)]
            ),
            Decimal(0),
        )
    )
    reserved += money(
        sum(
            (
                auth.amount
                for auth_id, status in seen_auths.items()
                if status not in {"FAILED", "CONFIRMED"}
                for auth in [next(a for a, _ in auth_rows if a.id == auth_id)]
            ),
            Decimal(0),
        )
    )
    component_available = {
        c.id: max(
            money(0), money(c.amount - confirmed_by_component[c.id] - reserved_by_component[c.id])
        )
        for c in components
    }
    verified_outstanding = available_to_authorize(expected, confirmed_paid, money(0))
    available = available_to_authorize(expected, confirmed_paid, reserved)
    return Verification(
        period_id,
        expected,
        money(confirmed_paid),
        money(reserved),
        verified_outstanding,
        available,
        component_available,
        uncertain,
        "; ".join(reasons) or None,
        evidence,
    )


def select_component_allocations(
    components: list[EarningComponent],
    verification: Verification,
    category: str,
    trip_id: str | None,
) -> dict[str, Decimal]:
    if category == "SURGE":
        eligible = [c for c in components if c.kind == "SURGE" and c.trip_id == trip_id]
    elif category == "BASE":
        eligible = [c for c in components if c.kind == "BASE" and c.trip_id == trip_id]
    elif category == "INCENTIVE":
        eligible = [c for c in components if c.kind == "INCENTIVE"]
    elif category == "TRIP":
        eligible = [c for c in components if c.trip_id == trip_id]
    elif category in {"TOTAL", "PARTIAL", "PAYOUT"}:
        eligible = [c for c in components if c.trip_id == trip_id] if trip_id else components
    else:
        eligible = []
    remaining = verification.available
    result: dict[str, Decimal] = {}
    for component in sorted(eligible, key=lambda c: c.id):
        value = min(verification.component_available.get(component.id, money(0)), remaining)
        if value > 0:
            result[component.id] = money(value)
            remaining -= value
    return result
