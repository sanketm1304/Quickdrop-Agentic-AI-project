"""Durable inbound messages and deterministic dispute investigation."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent import (
    DeterministicAgentClient,
    GeminiClient,
    QuickDropAgent,
    SqlMemoryStore,
)
from app.agent.schemas import AgentResult, IssueState
from app.agent_tools import make_tool_executor
from app.config import Settings
from app.database import SessionLocal
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import (
    AuditEvent,
    Conversation,
    Deduction,
    Dispute,
    DisputeEvidence,
    DisputeRevision,
    EarningComponent,
    EarningPeriod,
    Job,
    Message,
    Rider,
    Trip,
)
from app.policy import AUTO_LIMIT, POLICY_VERSION, money
from app.services.financial import select_component_allocations, verify_period
from app.services.payments import category_for_components, reserve_correction

ACK = "Your message is saved. We are checking the records; refresh for the result."
SIMPLE_STATUS_REQUESTS = {
    "status",
    "status update",
    "case status",
    "my case status",
    "payment status",
    "payout status",
    "what is my status",
    "what is my case status",
    "what is my payout status",
    "what's my status",
    "mera status kya hai",
    "payout ka status kya hai",
    "मेरा स्टेटस क्या है",
}


def _is_simple_status_request(text: str) -> bool:
    return " ".join(text.casefold().strip().rstrip("?!।").split()) in SIMPLE_STATUS_REQUESTS


async def _status_reply(session, rider_id: str) -> str:
    cases = (
        await session.scalars(
            select(Dispute)
            .where(Dispute.rider_id == rider_id)
            .order_by(Dispute.updated_at.desc())
            .limit(3)
        )
    ).all()
    if not cases:
        return "No prior dispute was found for your account."
    labels = {
        "PAID": "simulated correction recorded; check the portal for current provider confirmation",
        "PENDING_APPROVAL": "awaiting employee approval",
        "UNDER_REVIEW": "under employee review",
        "RECONCILIATION_REVIEW": "under payment reconciliation review",
        "PROCESSING": "simulated correction processing",
        "RECONCILING": "simulated payment verification in progress",
        "NO_BALANCE": "no additional balance found",
        "PAYMENT_FAILED": "simulated payment failed",
        "NEEDS_CLARIFICATION": "needs clarification",
    }
    summary = "; ".join(
        f"{case.category.replace('_', ' ')}: "
        f"{labels.get(case.status, case.status.lower().replace('_', ' '))}"
        for case in cases
    )
    return f"Latest recorded case status: {summary}."


async def enqueue_message(
    *,
    message_id: str,
    rider_id: str,
    text: str,
    received_at: datetime,
) -> str:
    if not text.strip() or len(text) > 2000:
        raise ValueError("message must contain 1–2000 characters")
    if received_at.tzinfo is None:
        raise ValueError("received_at must include a timezone")
    try:
        async with SessionLocal.begin() as session:
            rider = await session.get(Rider, rider_id)
            if rider is None or rider.status != "ACTIVE":
                raise LookupError("rider not found")
            existing = await session.scalar(select(Message).where(Message.message_id == message_id))
            if existing:
                if existing.rider_id != rider_id or existing.text != text:
                    raise ValueError("message ID already belongs to different content")
                return existing.reply or ACK
            conversation = await session.scalar(
                select(Conversation).where(Conversation.rider_id == rider_id)
            )
            if conversation is None:
                conversation = Conversation(rider_id=rider_id)
                session.add(conversation)
                await session.flush()
            incoming = Message(
                message_id=message_id,
                rider_id=rider_id,
                conversation_id=conversation.id,
                sender="rider",
                text=text,
                received_at=received_at,
                status="QUEUED",
            )
            session.add(incoming)
            await session.flush()
            session.add(
                AuditEvent(
                    rider_id=rider_id,
                    event_type="MESSAGE_RECEIVED",
                    actor=f"rider:{rider_id}",
                    details={"message_id": message_id},
                )
            )
            if _is_simple_status_request(text):
                older_work = await session.scalar(
                    select(Job.id).where(
                        Job.kind == "MESSAGE",
                        Job.rider_id == rider_id,
                        Job.status.in_(["READY", "RUNNING"]),
                    )
                )
                if older_work is None:
                    reply = await _status_reply(session, rider_id)
                    incoming.reply = reply
                    incoming.status = "PROCESSED"
                    session.add(
                        AuditEvent(
                            rider_id=rider_id,
                            event_type="RIDER_REPLY",
                            actor="application",
                            details={"message_id": message_id, "reply": reply},
                        )
                    )
                    return reply
            session.add(
                Job(
                    kind="MESSAGE",
                    key=f"MESSAGE:{message_id}",
                    rider_id=rider_id,
                    source_sequence=incoming.sequence,
                    payload={"message_id": message_id},
                )
            )
            return ACK
    except IntegrityError:
        async with SessionLocal() as session:
            existing = await session.scalar(select(Message).where(Message.message_id == message_id))
            if existing and existing.rider_id == rider_id and existing.text == text:
                return existing.reply or ACK
        raise


async def _upsert_dispute(issue: IssueState, message: Message) -> tuple[str, str]:
    """Persist a model interpretation, then verify source evidence independently."""
    async with SessionLocal.begin() as session:
        dispute = await session.get(Dispute, issue.issue_id)
        was_settled = bool(dispute and dispute.status == "PAID")
        changed = any(revision.message_id == message.message_id for revision in issue.revisions)
        unchanged_previous_status = dispute.status if dispute and not changed else None
        if dispute is None:
            dispute = Dispute(
                id=issue.issue_id,
                rider_id=message.rider_id,
                conversation_id=message.conversation_id,
                source_message_id=message.message_id,
                category=issue.category.value,
                trip_id=None,
                earning_date=issue.earning_date,
                claimed_amount=issue.claimed_amount,
                status="NEW",
                revision=1,
            )
            session.add(dispute)
            # Child audit and revision rows use scalar FK IDs, so establish the
            # parent row before SQLAlchemy flushes them at commit.
            await session.flush()
        else:
            if dispute.rider_id != message.rider_id:
                raise ValueError("cross-rider dispute identity")
            dispute.revision += 1
            dispute.status = unchanged_previous_status or "NEW"
            dispute.category = issue.category.value
            dispute.claimed_amount = issue.claimed_amount
            dispute.earning_date = issue.earning_date or dispute.earning_date
        dispute.trip_id = None
        dispute.period_id = None
        trip = None
        if issue.trip_id:
            trip = await session.scalar(
                select(Trip).where(Trip.id == issue.trip_id, Trip.rider_id == message.rider_id)
            )
            if trip is None:
                dispute.status = "INVALID_REFERENCE"
                dispute.explanation = (
                    "That trip is not in your QuickDrop account. Please check the trip ID."
                )
            else:
                dispute.trip_id = trip.id
                dispute.earning_date = trip.trip_date
        if dispute.status != "INVALID_REFERENCE":
            if dispute.earning_date:
                period = await session.scalar(
                    select(EarningPeriod).where(
                        EarningPeriod.rider_id == message.rider_id,
                        EarningPeriod.business_date == dispute.earning_date,
                    )
                )
                dispute.period_id = period.id if period else None
                if period is None:
                    dispute.status = "NEEDS_CLARIFICATION"
                    dispute.explanation = (
                        "No earning period was found for that date. Please check it."
                    )
            else:
                dispute.status = "NEEDS_CLARIFICATION"
                dispute.explanation = "Please give a trip ID or the earning date to check."
        session.add(
            DisputeRevision(
                dispute_id=dispute.id,
                message_id=message.message_id,
                revision=dispute.revision,
                claimed_amount=issue.claimed_amount,
                trip_id=issue.trip_id,
                category=issue.category.value,
            )
        )
        session.add(
            AuditEvent(
                rider_id=message.rider_id,
                dispute_id=dispute.id,
                event_type="DISPUTE_REVISED" if dispute.revision > 1 else "DISPUTE_OPENED",
                actor="agent_interpretation",
                details={
                    "message_id": message.message_id,
                    "revision": dispute.revision,
                    "category": dispute.category,
                    "trip_id": dispute.trip_id,
                    "earning_date": dispute.earning_date.isoformat()
                    if dispute.earning_date
                    else None,
                    "claimed_amount": str(dispute.claimed_amount)
                    if dispute.claimed_amount is not None
                    else None,
                },
            )
        )
        return dispute.id, (
            "UNCHANGED:" + unchanged_previous_status
        ) if unchanged_previous_status else "SETTLED_CORRECTION" if was_settled else dispute.status


async def _investigate(
    issue: IssueState,
    message: Message,
    provider: MockPaySwiftClient,
    model_review: bool,
) -> str:
    dispute_id, initial_status = await _upsert_dispute(issue, message)
    if initial_status.startswith("UNCHANGED:"):
        status = initial_status.removeprefix("UNCHANGED:")
        return f"This case is already {status.lower().replace('_', ' ')}. No duplicate correction was started."
    async with SessionLocal() as session:
        dispute = await session.get(Dispute, dispute_id)
        if dispute.status in {"INVALID_REFERENCE", "NEEDS_CLARIFICATION"}:
            return dispute.explanation or "Please clarify this complaint."
        period_id, trip_id, category = dispute.period_id, dispute.trip_id, dispute.category
    if category == "deduction":
        async with SessionLocal() as session:
            verification = await verify_period(session, period_id, provider)
            deductions = (
                await session.scalars(select(Deduction).where(Deduction.period_id == period_id))
            ).all()
        explained = bool(deductions) and not verification.uncertain and verification.available == 0
        async with SessionLocal.begin() as session:
            dispute = await session.get(Dispute, dispute_id)
            dispute.verified_outstanding = verification.verified_outstanding
            dispute.available_to_authorize = money(0)
            dispute.status = "NO_BALANCE" if explained else "UNDER_REVIEW"
            dispute.explanation = (
                "Recorded deductions are supported by evidence and the net payout is confirmed."
                if explained
                else "Deduction or provider records need employee review."
            )
            session.add(
                DisputeEvidence(
                    dispute_id=dispute.id,
                    kind="DEDUCTION_CHECK",
                    reference=period_id,
                    details={
                        "deductions": [
                            {
                                "id": item.id,
                                "amount": str(item.amount),
                                "category": item.category,
                                "reason": item.reason,
                                "evidence_reference": item.evidence_reference,
                            }
                            for item in deductions
                        ],
                        "expected": str(verification.expected),
                        "confirmed_paid": str(verification.confirmed_paid),
                        "provider_uncertain": verification.uncertain,
                    },
                )
            )
            session.add(
                AuditEvent(
                    rider_id=message.rider_id,
                    dispute_id=dispute.id,
                    event_type="FINANCIAL_CALCULATION",
                    actor="policy_engine",
                    details={
                        "period_id": period_id,
                        "verified_outstanding": str(verification.verified_outstanding),
                        "issue_available": "0.00" if explained else str(verification.available),
                        "uncertain": verification.uncertain,
                        "case_status": dispute.status,
                    },
                )
            )
        if explained:
            return "The recorded deduction has supporting evidence, and the net simulated payout is confirmed. No additional balance is owed."
        return "The deduction or settlement needs employee review. No correction was authorized."
    if category in {"delayed_payout", "duplicate_payout"}:
        async with SessionLocal() as session:
            verification = await verify_period(session, period_id, provider)
        settled_delay = (
            category == "delayed_payout"
            and not verification.uncertain
            and verification.verified_outstanding == 0
        )
        async with SessionLocal.begin() as session:
            dispute = await session.get(Dispute, dispute_id)
            dispute.verified_outstanding = verification.verified_outstanding
            dispute.reserved_amount = verification.reserved
            dispute.available_to_authorize = money(0)
            dispute.status = "NO_BALANCE" if settled_delay else "UNDER_REVIEW"
            dispute.explanation = (
                "The simulated provider confirms this payout; no additional balance is owed."
                if settled_delay
                else "The payout timing or transaction history needs employee review."
            )
            session.add(
                DisputeEvidence(
                    dispute_id=dispute.id,
                    kind="PROVIDER_CHECK",
                    reference=period_id,
                    details={
                        "policy_version": POLICY_VERSION,
                        "expected": str(verification.expected),
                        "confirmed_paid": str(verification.confirmed_paid),
                        "verified_outstanding": str(verification.verified_outstanding),
                        "provider_uncertain": verification.uncertain,
                        "provider_evidence": verification.evidence,
                    },
                )
            )
            session.add(
                AuditEvent(
                    rider_id=message.rider_id,
                    dispute_id=dispute.id,
                    event_type="FINANCIAL_CALCULATION",
                    actor="policy_engine",
                    details={
                        "period_id": period_id,
                        "verified_outstanding": str(verification.verified_outstanding),
                        "issue_available": "0.00",
                        "uncertain": verification.uncertain,
                        "case_status": dispute.status,
                    },
                )
            )
        if settled_delay:
            return "The simulated provider confirms this payout. No additional correction is owed."
        return "The simulated payout needs employee review. No correction was authorized."
    if category_for_components(category) == "UNSUPPORTED":
        async with SessionLocal.begin() as session:
            dispute = await session.get(Dispute, dispute_id)
            dispute.status = "UNDER_REVIEW"
            dispute.explanation = "This issue needs employee investigation."
        return "This issue needs employee investigation. No correction has been authorized."
    async with SessionLocal() as session:
        verification = await verify_period(session, period_id, provider)
        components = list(
            (
                await session.scalars(
                    select(EarningComponent).where(EarningComponent.period_id == period_id)
                )
            ).all()
        )
        selected = select_component_allocations(
            components, verification, category_for_components(category), trip_id
        )
        amount = (
            money(sum(selected.values(), Decimal(0))) if not verification.uncertain else money(0)
        )
    async with SessionLocal.begin() as session:
        dispute = await session.get(Dispute, dispute_id)
        dispute.verified_outstanding = verification.verified_outstanding
        dispute.reserved_amount = verification.reserved
        dispute.available_to_authorize = amount
        session.add(
            DisputeEvidence(
                dispute_id=dispute.id,
                kind="CALCULATION",
                reference=period_id,
                details={
                    "policy_version": POLICY_VERSION,
                    "expected": str(verification.expected),
                    "confirmed_paid": str(verification.confirmed_paid),
                    "reserved": str(verification.reserved),
                    "issue_available": str(amount),
                    "uncertain": verification.uncertain,
                    "provider_evidence": verification.evidence,
                },
            )
        )
        session.add(
            AuditEvent(
                rider_id=message.rider_id,
                dispute_id=dispute.id,
                event_type="FINANCIAL_CALCULATION",
                actor="policy_engine",
                details={
                    "period_id": period_id,
                    "verified_outstanding": str(verification.verified_outstanding),
                    "issue_available": str(amount),
                    "uncertain": verification.uncertain,
                    "case_status": "UNDER_REVIEW"
                    if verification.uncertain
                    else "NO_BALANCE"
                    if amount <= 0
                    else "PENDING_APPROVAL"
                    if initial_status == "SETTLED_CORRECTION"
                    else "UNDER_REVIEW"
                    if model_review or issue.requires_review
                    else "PENDING_APPROVAL"
                    if amount > AUTO_LIMIT
                    else "NO_BALANCE"
                    if amount <= 0
                    else "AUTO_ELIGIBLE",
                },
            )
        )
        if verification.uncertain:
            dispute.status = "UNDER_REVIEW"
            dispute.explanation = verification.reason or "Provider settlement is unclear."
        elif amount <= 0:
            dispute.status = "NO_BALANCE"
            dispute.explanation = (
                "Verified records show no available unpaid balance for this issue."
            )
        elif initial_status == "SETTLED_CORRECTION":
            dispute.status = "PENDING_APPROVAL"
            dispute.explanation = (
                "Fresh source and simulated provider records show an additional balance; "
                "the corrected claim requires employee approval."
            )
        elif model_review or issue.requires_review:
            dispute.status = "UNDER_REVIEW"
            dispute.explanation = "Complaint details need employee review."
        elif amount > AUTO_LIMIT:
            dispute.status = "PENDING_APPROVAL"
            dispute.explanation = "Verified balance exceeds the automatic correction limit."
    if verification.uncertain:
        return "The simulated provider record is unclear, so an employee will review this claim."
    if amount <= 0:
        return "I checked the source earnings and confirmed simulated payouts; no unpaid balance remains for this issue."
    if initial_status == "SETTLED_CORRECTION":
        return (
            f"I rechecked the source earnings and simulated provider records. "
            f"An additional ₹{amount:.2f} is available for this corrected claim; "
            "employee approval is required before another simulated payment."
        )
    if model_review or issue.requires_review:
        return "Your claim needs employee review before any simulated correction."
    if amount > AUTO_LIMIT:
        return f"I verified ₹{amount:.2f} outstanding. Employee approval is required."
    outcome, current = await reserve_correction(dispute_id, provider, mode="AUTO")
    if outcome == "RESERVED":
        return f"I verified ₹{current:.2f} outstanding. A simulated correction is being processed; settlement is not confirmed yet."
    if outcome == "REQUIRES_APPROVAL":
        return f"I verified ₹{current:.2f} outstanding. Employee approval is required for this correction."
    if outcome == "NO_BALANCE":
        return "Another correction already covers this balance; no duplicate payment was started."
    return "This correction needs employee review before payment can proceed."


async def process_message(message_id: str, provider: MockPaySwiftClient, settings: Settings) -> str:
    async with SessionLocal() as session:
        message = await session.scalar(select(Message).where(Message.message_id == message_id))
        if message is None:
            raise LookupError("message not found")
        if message.status == "PROCESSED":
            return message.reply or ACK
        if _is_simple_status_request(message.text):
            reply = await _status_reply(session, message.rider_id)
            message.reply = reply
            message.status = "PROCESSED"
            session.add(
                AuditEvent(
                    rider_id=message.rider_id,
                    event_type="RIDER_REPLY",
                    actor="application",
                    details={"message_id": message_id, "reply": reply},
                )
            )
            await session.commit()
            return reply
        if message.received_at.tzinfo is None:
            message.received_at = message.received_at.replace(tzinfo=UTC)
        client = (
            GeminiClient(settings.gemini_api_key, settings.gemini_model)
            if settings.agent_mode == "gemini"
            else DeterministicAgentClient()
        )
        agent = QuickDropAgent(client, make_tool_executor(provider), SqlMemoryStore(session))
        result: AgentResult = await agent.handle_message(
            message.rider_id,
            message.conversation_id,
            message.message_id,
            message.text,
            message.received_at,
        )
        session.expunge(message)
    affected = [issue for issue in result.issues if message_id in issue.source_message_ids]
    statements: list[str] = []
    if result.deferred and not affected:
        async with SessionLocal.begin() as session:
            session.add(
                Dispute(
                    rider_id=message.rider_id,
                    conversation_id=message.conversation_id,
                    source_message_id=message.message_id,
                    category="other",
                    status="UNDER_REVIEW",
                    explanation="Agent unavailable; employee review required.",
                )
            )
        statements.append(
            "Your message is saved for employee review. No correction was authorized."
        )
    else:
        for issue in affected:
            statements.append(
                await _investigate(
                    issue, message, provider, result.requires_review or result.deferred
                )
            )
    if not statements:
        if result.extraction.status_request:
            async with SessionLocal() as session:
                rows = (
                    await session.scalars(
                        select(Dispute)
                        .where(Dispute.rider_id == message.rider_id)
                        .order_by(Dispute.updated_at.desc())
                        .limit(3)
                    )
                ).all()
                statements.append(
                    "Current case status: "
                    + ", ".join(
                        f"{row.category} {row.status.lower().replace('_', ' ')}" for row in rows
                    )
                    if rows
                    else "No prior dispute was found for your account."
                )
        else:
            statements.append(result.reply)
    reply = " ".join(statements)[:2000]
    async with SessionLocal.begin() as session:
        stored = await session.scalar(
            select(Message).where(Message.message_id == message_id).with_for_update()
        )
        if stored.status != "PROCESSED":
            stored.reply = reply
            stored.status = "PROCESSED"
            for trace in result.tool_trace:
                session.add(
                    AuditEvent(
                        rider_id=stored.rider_id,
                        event_type="AGENT_TOOL",
                        actor="single_agent",
                        details={
                            "name": trace.name,
                            "success": trace.success,
                            "arguments": trace.arguments,
                        },
                    )
                )
            session.add(
                AuditEvent(
                    rider_id=stored.rider_id,
                    event_type="RIDER_REPLY",
                    actor="application",
                    details={"message_id": message_id, "reply": reply},
                )
            )
    return reply
