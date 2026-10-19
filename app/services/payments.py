"""Transaction-safe simulated correction authorization and provider reconciliation."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.integrations.mockpayswift_client import (
    MockPaySwiftClient,
    ProviderProtocolError,
    ProviderUnavailable,
)
from app.models import (
    Approval,
    AuditEvent,
    Dispute,
    EarningAdjustmentAllocation,
    EarningComponent,
    EarningPeriod,
    Employee,
    Job,
    OutboxEvent,
    PaymentAttempt,
    PaymentAuthorization,
    PaymentReconciliation,
    PayoutAllocation,
    PayoutRecord,
    Rider,
    uid,
)
from app.policy import AUTO_LIMIT, POLICY_VERSION, business_date, money
from app.services.financial import Verification, select_component_allocations, verify_period

ACTIVE = {"RESERVED", "SUBMITTING", "SUBMITTED", "RECONCILING", "CONFIRMED", "OPS_REVIEW"}
_reconcile_cursor: tuple[datetime, str] | None = None
_reconcile_lock = asyncio.Lock()
PAYMENT_TRANSITIONS = {
    "RESERVED": {"SUBMITTING", "RECONCILING", "FAILED", "CONFIRMED", "OPS_REVIEW"},
    "SUBMITTING": {"SUBMITTED", "RECONCILING", "FAILED", "CONFIRMED", "OPS_REVIEW"},
    "SUBMITTED": {"RECONCILING", "FAILED", "CONFIRMED", "OPS_REVIEW"},
    "RECONCILING": {"RECONCILING", "FAILED", "CONFIRMED", "OPS_REVIEW"},
    "OPS_REVIEW": {"RECONCILING", "FAILED", "CONFIRMED"},
    "CONFIRMED": {"OPS_REVIEW"},
    "FAILED": {"OPS_REVIEW"},
}


def transition(auth: PaymentAuthorization, target: str) -> None:
    if target != auth.status and target not in PAYMENT_TRANSITIONS.get(auth.status, set()):
        raise ValueError(f"invalid payment transition {auth.status} -> {target}")
    auth.status = target


def category_for_components(category: str) -> str:
    return {
        "surge": "SURGE",
        "incentive": "INCENTIVE",
        "base_pay": "BASE",
        "total_payout": "TOTAL",
        "partial_payout": "TOTAL",
    }.get(category, "UNSUPPORTED")


async def _locked_available(
    session: AsyncSession,
    period: EarningPeriod,
    category: str,
    trip_id: str | None,
) -> tuple[dict[str, Decimal], Decimal]:
    components = list(
        (
            await session.scalars(
                select(EarningComponent)
                .where(EarningComponent.period_id == period.id)
                .order_by(EarningComponent.id)
            )
        ).all()
    )
    payouts = list(
        (
            await session.scalars(select(PayoutRecord).where(PayoutRecord.period_id == period.id))
        ).all()
    )
    baseline_paid = money(sum((p.amount for p in payouts), Decimal(0)))
    baseline: dict[str, Decimal] = {c.id: money(0) for c in components}
    for allocation in (
        await session.scalars(
            select(PayoutAllocation).join(PayoutRecord).where(PayoutRecord.period_id == period.id)
        )
    ).all():
        baseline[allocation.component_id] += money(allocation.amount)
    auth_rows = (
        await session.execute(
            select(PaymentAuthorization, EarningAdjustmentAllocation)
            .join(
                EarningAdjustmentAllocation,
                EarningAdjustmentAllocation.authorization_id == PaymentAuthorization.id,
            )
            .join(EarningComponent, EarningComponent.id == EarningAdjustmentAllocation.component_id)
            .where(EarningComponent.period_id == period.id, PaymentAuthorization.status.in_(ACTIVE))
        )
    ).all()
    allocated: dict[str, Decimal] = {c.id: money(0) for c in components}
    active_auths: dict[str, PaymentAuthorization] = {}
    for auth, allocation in auth_rows:
        allocated[allocation.component_id] += money(allocation.amount)
        active_auths[auth.id] = auth
    cap = max(
        money(0),
        money(
            period.expected_amount
            - baseline_paid
            - sum((a.amount for a in active_auths.values()), Decimal(0))
        ),
    )
    synthetic = Verification(
        period.id,
        period.expected_amount,
        baseline_paid,
        money(0),
        cap,
        cap,
        {
            c.id: max(money(0), money(c.amount - baseline[c.id] - allocated[c.id]))
            for c in components
        },
        False,
        None,
        [],
    )
    return select_component_allocations(components, synthetic, category, trip_id), cap


async def reserve_correction(
    dispute_id: str,
    provider: MockPaySwiftClient,
    *,
    mode: str,
    expected_revision: int | None = None,
    reviewer_id: str | None = None,
    review_reason: str | None = None,
) -> tuple[str, Decimal | None]:
    """Observe provider first, then lock and recheck all local allocations atomically."""
    async with SessionLocal() as session:
        dispute = await session.get(Dispute, dispute_id)
        if dispute is None or not dispute.period_id:
            return "UNVERIFIABLE", None
        observation = await verify_period(session, dispute.period_id, provider)
        if observation.uncertain:
            return "UNCERTAIN", None
        await session.rollback()
    async with SessionLocal.begin() as session:
        dispute = await session.scalar(
            select(Dispute).where(Dispute.id == dispute_id).with_for_update()
        )
        if dispute is None or not dispute.period_id:
            return "UNVERIFIABLE", None
        if expected_revision is not None and dispute.revision != expected_revision:
            return "STALE", None
        if mode == "HUMAN":
            if dispute.status not in {"PENDING_APPROVAL", "UNDER_REVIEW"}:
                return "STALE", None
            if not reviewer_id or not review_reason or len(review_reason.strip()) < 10:
                raise ValueError("human authorization needs recorded reviewer and reason")
            employee = await session.scalar(select(Employee).where(Employee.id == reviewer_id))
            if (
                employee is None
                or not employee.active
                or employee.role not in {"reviewer", "admin"}
            ):
                return "UNAUTHORIZED", None
        elif dispute.status in {"REJECTED", "PAID", "PROCESSING", "RECONCILING"}:
            return "STALE", None
        if session.bind.dialect.name == "postgresql":
            # A transaction-scoped lock serializes every correction for this
            # rider without granting UPDATE on immutable identity/source rows.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"quickdrop:correction:{dispute.rider_id}"},
            )
        else:
            await session.scalar(
                select(Rider).where(Rider.id == dispute.rider_id).with_for_update()
            )
        period = await session.get(EarningPeriod, dispute.period_id)
        if period is None or period.policy_version != POLICY_VERSION:
            return "UNVERIFIABLE", None
        local_selected, cap = await _locked_available(
            session, period, category_for_components(dispute.category), dispute.trip_id
        )
        observed_cap = min(cap, observation.available)
        selected = {}
        for component_id, local_amount in local_selected.items():
            value = min(
                local_amount,
                observation.component_available.get(component_id, money(0)),
                observed_cap,
            )
            if value > 0:
                selected[component_id] = money(value)
                observed_cap -= value
        amount = money(sum(selected.values(), Decimal(0)))
        if amount <= 0 or cap <= 0:
            dispute.status = "NO_BALANCE"
            dispute.verified_outstanding = money(0)
            dispute.available_to_authorize = money(0)
            return "NO_BALANCE", money(0)
        if mode == "AUTO":
            if amount > AUTO_LIMIT:
                dispute.status = "PENDING_APPROVAL"
                return "REQUIRES_APPROVAL", amount
            previous = await session.scalar(
                select(PaymentAuthorization.id).where(
                    PaymentAuthorization.rider_id == dispute.rider_id,
                    PaymentAuthorization.business_date == business_date(datetime.now(UTC)),
                    PaymentAuthorization.mode == "AUTO",
                    PaymentAuthorization.status.in_(ACTIVE),
                )
            )
            if previous:
                dispute.status = "PENDING_APPROVAL"
                return "REQUIRES_APPROVAL", amount
        elif mode != "HUMAN":
            raise ValueError("invalid authorization mode")
        if mode == "HUMAN" and money(dispute.available_to_authorize) != amount:
            return "STALE", None
        auth_id = uid()
        auth = PaymentAuthorization(
            id=auth_id,
            dispute_id=dispute.id,
            rider_id=dispute.rider_id,
            business_date=business_date(datetime.now(UTC)),
            mode=mode,
            status="RESERVED",
            amount=amount,
            policy_version=POLICY_VERSION,
            idempotency_key=f"QD-AUTH-{auth_id}",
        )
        session.add(auth)
        # Explicitly persist the parent before child allocations. SQLAlchemy
        # cannot infer this dependency from bare foreign-key IDs alone, and
        # PostgreSQL enforces the constraint at each INSERT statement.
        await session.flush()
        for component_id, value in selected.items():
            session.add(
                EarningAdjustmentAllocation(
                    authorization_id=auth_id,
                    component_id=component_id,
                    amount=value,
                    status="RESERVED",
                )
            )
        dispute.status = "PROCESSING"
        dispute.verified_outstanding = observation.verified_outstanding
        dispute.reserved_amount = amount
        dispute.available_to_authorize = money(0)
        dispute.authorized_amount = amount
        session.add(
            Job(
                kind="PAYMENT",
                key=f"PAYMENT:{auth_id}",
                rider_id=dispute.rider_id,
                payload={"authorization_id": auth_id},
            )
        )
        session.add(
            OutboxEvent(
                event_type="PAYMENT_RESERVED",
                aggregate_id=auth_id,
                payload={"dispute_id": dispute.id, "amount": str(amount)},
            )
        )
        session.add(
            AuditEvent(
                rider_id=dispute.rider_id,
                dispute_id=dispute.id,
                event_type="PAYMENT_RESERVED",
                actor="policy_engine",
                details={
                    "authorization_id": auth_id,
                    "amount": str(amount),
                    "mode": mode,
                    "policy_version": POLICY_VERSION,
                },
            )
        )
        if mode == "HUMAN":
            session.add(
                Approval(
                    dispute_id=dispute.id,
                    employee_id=reviewer_id,
                    revision=dispute.revision,
                    decision="APPROVED",
                    reason=review_reason.strip(),
                )
            )
            session.add(
                AuditEvent(
                    rider_id=dispute.rider_id,
                    dispute_id=dispute.id,
                    event_type="HUMAN_APPROVED",
                    actor=f"employee:{reviewer_id}",
                    details={
                        "revision": dispute.revision,
                        "reason": review_reason.strip(),
                        "authorization_id": auth_id,
                    },
                )
            )
        return "RESERVED", amount


async def execute_payment(authorization_id: str, provider: MockPaySwiftClient) -> str:
    """Retry only by the stable key; a lost HTTP response cannot cause a second transfer."""
    async with SessionLocal() as session:
        auth = await session.get(PaymentAuthorization, authorization_id)
        if auth is None:
            raise LookupError("authorization not found")
        terminal_status = auth.status if auth.status in {"CONFIRMED", "FAILED"} else None
        key, rider_id, dispute_id, amount = (
            auth.idempotency_key,
            auth.rider_id,
            auth.dispute_id,
            auth.amount,
        )
    try:
        payment = await provider.by_key(key)
        if payment is None and terminal_status is None:
            payment = await provider.submit(
                key=key, rider_id=rider_id, dispute_reference=dispute_id, amount=amount
            )
    except (ProviderUnavailable, ProviderProtocolError):
        try:
            payment = await provider.by_key(key)
        except (ProviderUnavailable, ProviderProtocolError):
            payment = None
    if (
        terminal_status
        and payment
        and payment.status == terminal_status
        and (
            payment.idempotency_key == key
            and payment.rider_id == rider_id
            and payment.dispute_reference == dispute_id
            and payment.amount == money(amount)
        )
    ):
        return terminal_status
    if terminal_status:
        status = "OPS_REVIEW"
    elif payment is None:
        status = "RECONCILING"
    elif (
        payment.idempotency_key != key
        or payment.rider_id != rider_id
        or payment.dispute_reference != dispute_id
        or payment.amount != money(amount)
    ):
        status = "OPS_REVIEW"
    elif payment.status == "CONFIRMED":
        status = "CONFIRMED"
    elif payment.status == "FAILED":
        status = "FAILED"
    elif payment.status == "REVERSED":
        status = "OPS_REVIEW"
    else:
        status = "RECONCILING"
    async with SessionLocal.begin() as session:
        auth = await session.scalar(
            select(PaymentAuthorization)
            .where(PaymentAuthorization.id == authorization_id)
            .with_for_update()
        )
        if auth is None:
            raise LookupError("authorization not found")
        if auth.status in {"CONFIRMED", "FAILED"} and status != "OPS_REVIEW":
            return auth.status
        transition(auth, status)
        if payment:
            auth.provider_payment_id = payment.payment_id
        attempt_count = len(
            (
                await session.scalars(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.authorization_id == authorization_id
                    )
                )
            ).all()
        )
        session.add(
            PaymentAttempt(
                authorization_id=authorization_id,
                attempt=attempt_count + 1,
                result=status,
                error_code=None if payment else "NO_OBSERVATION",
            )
        )
        session.add(
            PaymentReconciliation(
                authorization_id=authorization_id,
                provider_payment_id=payment.payment_id if payment else None,
                provider_status=payment.status if payment else "UNKNOWN",
                details={"validated": status != "OPS_REVIEW"},
            )
        )
        dispute = await session.get(Dispute, auth.dispute_id)
        allocations = (
            await session.scalars(
                select(EarningAdjustmentAllocation).where(
                    EarningAdjustmentAllocation.authorization_id == authorization_id
                )
            )
        ).all()
        if status == "CONFIRMED":
            for allocation in allocations:
                allocation.status = "CONFIRMED"
            dispute.status = "PAID"
            dispute.reserved_amount = money(0)
            dispute.confirmed_paid_amount = money(dispute.confirmed_paid_amount + auth.amount)
        elif status == "FAILED":
            for allocation in allocations:
                allocation.status = "RELEASED"
            dispute.status = "PAYMENT_FAILED"
            dispute.reserved_amount = money(0)
        elif status == "OPS_REVIEW":
            dispute.status = "RECONCILIATION_REVIEW"
        else:
            dispute.status = "RECONCILING"
        session.add(
            AuditEvent(
                rider_id=auth.rider_id,
                dispute_id=auth.dispute_id,
                event_type="PAYMENT_RECONCILED",
                actor="worker",
                details={
                    "authorization_id": auth.id,
                    "provider_status": payment.status if payment else "UNOBSERVED",
                    "local_status": status,
                },
            )
        )
    return status


async def reconcile_existing(provider: MockPaySwiftClient, limit: int = 100) -> int:
    """Sweep terminal and stranded authorizations for later provider changes."""
    if limit < 1:
        raise ValueError("reconciliation limit must be positive")
    global _reconcile_cursor
    async with _reconcile_lock:
        async with SessionLocal() as session:
            base = select(PaymentAuthorization.id, PaymentAuthorization.updated_at).where(
                PaymentAuthorization.status.in_(
                    ["CONFIRMED", "FAILED", "OPS_REVIEW", "RECONCILING"]
                )
            )
            ordered = base.order_by(PaymentAuthorization.updated_at, PaymentAuthorization.id)
            if _reconcile_cursor is None:
                rows = (await session.execute(ordered.limit(limit))).all()
            else:
                timestamp, authorization_id = _reconcile_cursor
                after = or_(
                    PaymentAuthorization.updated_at > timestamp,
                    and_(
                        PaymentAuthorization.updated_at == timestamp,
                        PaymentAuthorization.id > authorization_id,
                    ),
                )
                rows = (await session.execute(ordered.where(after).limit(limit))).all()
                if len(rows) < limit:
                    before_or_at = or_(
                        PaymentAuthorization.updated_at < timestamp,
                        and_(
                            PaymentAuthorization.updated_at == timestamp,
                            PaymentAuthorization.id <= authorization_id,
                        ),
                    )
                    rows.extend(
                        (
                            await session.execute(
                                ordered.where(before_or_at).limit(limit - len(rows))
                            )
                        ).all()
                    )
        checked = 0
        for authorization_id, timestamp in rows:
            try:
                await execute_payment(authorization_id, provider)
                checked += 1
            except (ProviderUnavailable, ProviderProtocolError):
                pass
            finally:
                # Advance even when one provider lookup fails so later records
                # are still checked. Retrying the same key remains safe.
                _reconcile_cursor = (timestamp, authorization_id)
        return checked
