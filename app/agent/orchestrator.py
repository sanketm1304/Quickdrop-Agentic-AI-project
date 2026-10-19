"""Bounded single-agent controller. No payment method is exposed here."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .gemini_client import AgentClient
from .memory import MemoryStore, merge_extraction
from .schemas import AgentResult, ComplaintExtraction, ConversationState, ToolResult
from .tool_executor import ToolExecutor

_FALSE_SETTLEMENT = re.compile(
    r"\b(?:paid|settled|approved|transferred|payment sent|payout complete)\b|भुगतान हो गया|पैसे भेज दिए",
    re.IGNORECASE,
)


def _kolkata_timezone():
    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:
        # Windows Python may not ship IANA data. Modern Indian business dates
        # use UTC+05:30 with no daylight-saving transitions.
        return timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")


def _safe_acknowledgement(extraction: ComplaintExtraction) -> str:
    if extraction.clarification_question:
        return extraction.clarification_question
    if extraction.language in {"hindi", "hinglish", "mixed"}:
        return "Aapki baat note kar li hai. Records ki jaanch chal rahi hai; payment status alag se verify hoga."
    return "I’ve recorded your message. The records are being checked; payment status will be verified separately."


class QuickDropAgent:
    """Orchestrates one model and only read-only investigation callbacks.

    The caller authenticates ``rider_id`` and must commit durable memory and
    financial state in its own transaction. No model-provided identity is used.
    """

    def __init__(
        self,
        client: AgentClient,
        tools: ToolExecutor,
        memory: MemoryStore,
        *,
        max_tool_rounds: int = 3,
        max_tool_calls: int = 12,
        deadline_seconds: float = 20.0,
    ) -> None:
        if max_tool_rounds < 1 or max_tool_calls < 1 or deadline_seconds <= 0:
            raise ValueError("orchestration limits must be positive")
        self.client = client
        self.tools = tools
        self.memory = memory
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.deadline_seconds = deadline_seconds

    async def handle_message(
        self,
        rider_id: str,
        conversation_id: str,
        message_id: str,
        text: str,
        received_at: datetime,
    ) -> AgentResult:
        if not rider_id or not conversation_id or not message_id or not text.strip():
            raise ValueError("rider, conversation, message, and text are required")
        kolkata = _kolkata_timezone()
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=kolkata)
        received_at = received_at.astimezone(kolkata)
        state = await self.memory.load(rider_id, conversation_id)
        if state.rider_id != rider_id or state.conversation_id != conversation_id:
            raise ValueError("memory returned data for a different rider or conversation")
        if message_id in state.processed_message_ids:
            return AgentResult(
                reply=state.previous_reply_by_message.get(
                    message_id, "This message has already been received."
                ),
                extraction=ComplaintExtraction(),
                issues=state.issues,
                duplicate=True,
                processed_at=datetime.now(UTC),
            )
        try:
            async with asyncio.timeout(self.deadline_seconds):
                result = await self._process(state, message_id, text, received_at)
        except Exception:  # noqa: BLE001 - all model failures become a safe deferred result
            # Model or tool outages must never be interpreted as a verified payout.
            extraction = ComplaintExtraction()
            reply = "Your message is saved for further checking. Please view this conversation for updates."
            if message_id not in state.processed_message_ids:
                state.processed_message_ids.append(message_id)
            state.previous_reply_by_message[message_id] = reply
            await self.memory.save(state)
            return AgentResult(
                reply=reply,
                extraction=extraction,
                issues=state.issues,
                requires_review=True,
                deferred=True,
                processed_at=datetime.now(UTC),
            )
        state.previous_reply_by_message[message_id] = result.reply
        await self.memory.save(state)
        return result

    async def _process(
        self, state: ConversationState, message_id: str, text: str, received_at: datetime
    ) -> AgentResult:
        extraction = await self.client.extract(text, state, received_at)
        state, ambiguous = merge_extraction(state, extraction, message_id)
        if ambiguous:
            reply = (
                extraction.clarification_question
                or "Which complaint are you correcting? Please mention the trip or earning date."
            )
            return AgentResult(
                reply=reply,
                extraction=extraction,
                issues=state.issues,
                requires_review=True,
                clarification_needed=True,
                processed_at=datetime.now(UTC),
            )
        if extraction.clarification_question and not extraction.issues:
            return AgentResult(
                reply=extraction.clarification_question,
                extraction=extraction,
                issues=state.issues,
                clarification_needed=True,
                processed_at=datetime.now(UTC),
            )
        trace: list[ToolResult] = []
        seen: set[str] = set()
        requires_review = any(
            issue.requires_review
            for issue in state.issues
            if message_id in issue.source_message_ids
        )
        if extraction.issues or extraction.status_request:
            session = self.client.start_investigation(text, state, extraction)
            pending: list[ToolResult] | None = None
            for _ in range(self.max_tool_rounds):
                action = await session.next_action(pending)
                if not action.tool_calls:
                    break
                if len(action.tool_calls) + len(trace) > self.max_tool_calls:
                    requires_review = True
                    break
                pending = []
                for call in action.tool_calls:
                    key = json.dumps(call.model_dump(mode="json"), sort_keys=True)
                    if key in seen:
                        requires_review = True
                        continue
                    seen.add(key)
                    evidence = await self.tools.execute(state.rider_id, call)
                    trace.append(evidence)
                    pending.append(evidence)
                    if not evidence.success:
                        requires_review = True
                if not pending:
                    break
        try:
            reply = await self.client.compose_reply(text, state, extraction, trace)
        except Exception:  # noqa: BLE001 - model wording is optional after evidence collection
            reply = _safe_acknowledgement(extraction)
            requires_review = True
        if _FALSE_SETTLEMENT.search(reply):
            reply = _safe_acknowledgement(extraction)
            requires_review = True
        if any(not item.success for item in trace):
            requires_review = True
        return AgentResult(
            reply=reply[:1000],
            extraction=extraction,
            issues=state.issues,
            tool_trace=trace,
            requires_review=requires_review,
            clarification_needed=bool(extraction.clarification_question),
            processed_at=datetime.now(UTC),
        )
