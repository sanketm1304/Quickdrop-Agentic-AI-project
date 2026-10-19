"""Conversation memory and deterministic issue revision logic.

The application owns persistence. A PostgreSQL adapter can implement ``MemoryStore``
using its messages, disputes, and dispute_revisions tables. This module owns only
the in-memory state transition so model output cannot silently overwrite history.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from .schemas import (
    ComplaintExtraction,
    ConversationState,
    ExtractedIssue,
    IssueRevision,
    IssueState,
)


class MemoryStore(Protocol):
    async def load(self, rider_id: str, conversation_id: str) -> ConversationState: ...

    async def save(self, state: ConversationState) -> None: ...


class InMemoryMemoryStore:
    """Offline test double; the application must use durable storage in production."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], ConversationState] = {}

    async def load(self, rider_id: str, conversation_id: str) -> ConversationState:
        state = self._states.get((rider_id, conversation_id))
        return (
            deepcopy(state)
            if state
            else ConversationState(rider_id=rider_id, conversation_id=conversation_id)
        )

    async def save(self, state: ConversationState) -> None:
        self._states[(state.rider_id, state.conversation_id)] = deepcopy(state)


def _match_issue(state: ConversationState, item: ExtractedIssue) -> IssueState | None:
    if item.target_issue_id:
        return next(
            (issue for issue in state.issues if issue.issue_id == item.target_issue_id), None
        )
    if item.trip_id:
        matches = [
            issue
            for issue in state.issues
            if issue.trip_id == item.trip_id and issue.category == item.category
        ]
        if len(matches) == 1:
            return matches[0]
    if item.earning_date:
        matches = [
            issue
            for issue in state.issues
            if issue.category == item.category and issue.earning_date == item.earning_date
        ]
        if len(matches) == 1:
            return matches[0]
    if item.correction and item.category.value != "other":
        matches = [issue for issue in state.issues if issue.category == item.category]
        if len(matches) == 1:
            return matches[0]
    if item.correction and len(state.issues) == 1:
        return state.issues[0]
    return None


def merge_extraction(
    state: ConversationState, extraction: ComplaintExtraction, message_id: str
) -> tuple[ConversationState, bool]:
    """Merge model interpretation while preserving every changed claim as a revision.

    Returns ``(state, ambiguous)``. An ambiguous correction is left unapplied and
    must be clarified by the rider before financial workflow proceeds.
    """
    if message_id in state.processed_message_ids:
        return state, False
    ambiguous = False
    for index, item in enumerate(extraction.issues):
        issue = _match_issue(state, item)
        if item.correction and issue is None:
            ambiguous = True
            continue
        if issue is None:
            issue_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"quickdrop:{state.rider_id}:{state.conversation_id}:{message_id}:{index}",
                )
            )
            state.issues.append(
                IssueState(
                    issue_id=issue_id,
                    category=item.category,
                    trip_id=item.trip_id,
                    earning_date=item.earning_date,
                    original_claimed_amount=item.claimed_amount,
                    claimed_amount=item.claimed_amount,
                    source_message_ids=[message_id],
                    requires_review=item.clarification_needed or item.confidence < 0.65,
                )
            )
            continue
        previous = {
            "category": issue.category,
            "trip_id": issue.trip_id,
            "earning_date": issue.earning_date,
            "claimed_amount": issue.claimed_amount,
        }
        if item.correction:
            if item.category.value != "other":
                issue.category = item.category
            issue.trip_id = item.trip_id or issue.trip_id
            issue.earning_date = item.earning_date or issue.earning_date
            issue.claimed_amount = (
                item.claimed_amount if item.claimed_amount is not None else issue.claimed_amount
            )
        elif item.claimed_amount is not None and issue.claimed_amount != item.claimed_amount:
            # A contradictory repeat is a revision and needs a human-facing explanation.
            issue.claimed_amount = item.claimed_amount
            issue.requires_review = True
        if message_id not in issue.source_message_ids:
            issue.source_message_ids.append(message_id)
        current = {
            "category": issue.category,
            "trip_id": issue.trip_id,
            "earning_date": issue.earning_date,
            "claimed_amount": issue.claimed_amount,
        }
        if current != previous:
            issue.revisions.append(
                IssueRevision(
                    message_id=message_id,
                    previous=previous,
                    current=current,
                    reason="correction" if item.correction else "contradictory_repeat",
                )
            )
            if issue.status.lower() in {"settled", "confirmed", "paid", "resolved"}:
                issue.requires_review = True
        issue.requires_review = (
            issue.requires_review or item.clarification_needed or item.confidence < 0.65
        )
    state.processed_message_ids.append(message_id)
    return state, ambiguous


class SqlAlchemyMemoryStore:
    """Read committed PostgreSQL conversation state from application tables.

    ``save`` is intentionally a no-op. The domain message service persists the
    returned issue revisions and reply in the same transaction as its audit
    records. This adapter reads only committed rows; only messages with
    ``PROCESSED`` status count as already handled.
    """

    def __init__(self, session_or_factory: object) -> None:
        self.session_or_factory = session_or_factory

    @asynccontextmanager
    async def _session(self):
        from sqlalchemy.ext.asyncio import AsyncSession

        if isinstance(self.session_or_factory, AsyncSession):
            yield self.session_or_factory
        else:
            async with self.session_or_factory() as session:
                yield session

    async def load(self, rider_id: str, conversation_id: str) -> ConversationState:
        from sqlalchemy import select

        from app.models import Dispute, DisputeRevision, EarningPeriod, Message

        state = ConversationState(rider_id=rider_id, conversation_id=conversation_id)
        async with self._session() as session:
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.rider_id == rider_id, Message.conversation_id == conversation_id)
                    .order_by(Message.sequence)
                )
            ).all()
            for message in messages:
                if message.status == "PROCESSED":
                    state.processed_message_ids.append(message.message_id)
                    if message.reply is not None:
                        state.previous_reply_by_message[message.message_id] = message.reply
            disputes = (
                await session.scalars(
                    select(Dispute)
                    .where(Dispute.rider_id == rider_id, Dispute.conversation_id == conversation_id)
                    .order_by(Dispute.created_at, Dispute.id)
                )
            ).all()
            period_ids = {dispute.period_id for dispute in disputes if dispute.period_id}
            period_dates = {}
            if period_ids:
                periods = (
                    await session.scalars(
                        select(EarningPeriod).where(EarningPeriod.id.in_(period_ids))
                    )
                ).all()
                period_dates = {period.id: period.business_date for period in periods}
            dispute_ids = [dispute.id for dispute in disputes]
            revisions_by_dispute: dict[str, list[object]] = {}
            if dispute_ids:
                revisions = (
                    await session.scalars(
                        select(DisputeRevision)
                        .where(DisputeRevision.dispute_id.in_(dispute_ids))
                        .order_by(DisputeRevision.revision)
                    )
                ).all()
                for revision in revisions:
                    revisions_by_dispute.setdefault(revision.dispute_id, []).append(revision)
            for dispute in disputes:
                history = revisions_by_dispute.get(dispute.id, [])
                first = history[0] if history else None
                source_ids = [dispute.source_message_id]
                for revision in history:
                    if revision.message_id not in source_ids:
                        source_ids.append(revision.message_id)
                issue = IssueState(
                    issue_id=dispute.id,
                    category=_category_value(dispute.category),
                    trip_id=dispute.trip_id,
                    earning_date=getattr(dispute, "earning_date", None)
                    or period_dates.get(dispute.period_id),
                    original_claimed_amount=first.claimed_amount
                    if first
                    else dispute.claimed_amount,
                    claimed_amount=dispute.claimed_amount,
                    status=dispute.status,
                    source_message_ids=source_ids,
                    requires_review=dispute.status.upper() in {"OPS_REVIEW", "ESCALATED"},
                )
                previous = {
                    "category": _category_value(first.category) if first else issue.category,
                    "trip_id": first.trip_id if first else issue.trip_id,
                    "earning_date": issue.earning_date,
                    "claimed_amount": first.claimed_amount if first else issue.claimed_amount,
                }
                for revision in history[1:]:
                    current = {
                        "category": _category_value(revision.category),
                        "trip_id": revision.trip_id,
                        "earning_date": issue.earning_date,
                        "claimed_amount": revision.claimed_amount,
                    }
                    issue.revisions.append(
                        IssueRevision(
                            message_id=revision.message_id,
                            previous=previous,
                            current=current,
                            reason="persisted_revision",
                        )
                    )
                    previous = current
                state.issues.append(issue)
        return state

    async def save(self, state: ConversationState) -> None:
        # Persisted by the caller's domain transaction, never independently here.
        return None


def _category_value(value: str):
    from .schemas import IssueCategory

    try:
        return IssueCategory(value.lower())
    except ValueError:
        return IssueCategory.OTHER


SqlMemoryStore = SqlAlchemyMemoryStore
