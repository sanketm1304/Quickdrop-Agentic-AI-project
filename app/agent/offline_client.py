"""Deterministic offline substitute for development and regression tests.

This parser deliberately flags ambiguity; it is not a substitute for live Gemini
quality on unrestricted rider language. It never makes a financial decision.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from .gemini_client import InvestigationSession, ModelAction
from .schemas import (
    ComplaintExtraction,
    ConversationState,
    ExtractedIssue,
    IssueCategory,
    ToolCall,
    ToolResult,
)

_TRIP = re.compile(r"\bT\d{2,}\b", re.IGNORECASE)
_RUPEES = re.compile(r"(?:₹|\brs\.?\s*|\binr\s*)(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
_SHORT = re.compile(r"\b(\d[\d,]*(?:\.\d{1,2})?)\s*(?:kam|less|short|missing)\b", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_DAY = re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareekh|तारीख)\b", re.IGNORECASE)
_CORRECTION_MARKER = re.compile(
    r"\b(?:actually|sorry|instead|correction)\b|असल में|गलती|सुधार", re.IGNORECASE
)
_VALUE_CORRECTION = re.compile(
    r"(?:₹\s*)?(?<![A-Za-z0-9])\d+(?:\.\d{1,2})?\s+(?:nahi|nahin|not|नहीं)\s*[,;]?\s*"
    r"(?:₹\s*)?\d+(?:\.\d{1,2})?\b",
    re.IGNORECASE,
)
_TRIP_CORRECTION = re.compile(r"\bT\d+\s+(?:nahi|nahin|not|नहीं)\s+T\d+\b", re.IGNORECASE)
_STATUS = re.compile(r"\b(?:status|update|kya hua|what happened)\b|क्या हुआ|स्थिति", re.IGNORECASE)
_COMPLAINT = re.compile(
    r"\b(?:missing|kam|short|less|pending|incorrect|wrong|late|delay|deduct|surge|incentive|payout|payment|base|duplicate)\b|कम|नहीं|बकाया|भुगतान|प्रोत्साहन|कटौती",
    re.IGNORECASE,
)
_ROMAN_HINDI = re.compile(
    r"\b(?:bhai|kal|ka|ki|kam|nahi|mila|hai|aur|tarikh|karo|hua)\b", re.IGNORECASE
)


def _language(text: str) -> str:
    has_devanagari = bool(re.search(r"[\u0900-\u097f]", text))
    has_roman = bool(re.search(r"[A-Za-z]", text))
    if has_devanagari:
        return "mixed" if has_roman else "hindi"
    return "hinglish" if _ROMAN_HINDI.search(text) else "english"


def _category(text: str) -> IssueCategory:
    lower = text.lower()
    if "surge" in lower or "सर्ज" in text:
        return IssueCategory.SURGE
    if "incentive" in lower or "प्रोत्साहन" in text:
        return IssueCategory.INCENTIVE
    if "deduct" in lower or "कटौती" in text:
        return IssueCategory.DEDUCTION
    if "base" in lower:
        return IssueCategory.BASE_PAY
    if "duplicate" in lower or "twice" in lower:
        return IssueCategory.DUPLICATE_PAYOUT
    if "delay" in lower or "late" in lower:
        return IssueCategory.DELAYED_PAYOUT
    if "partial" in lower:
        return IssueCategory.PARTIAL_PAYOUT
    if "payout" in lower or "payment" in lower or "भुगतान" in text or "कम" in text:
        return IssueCategory.TOTAL_PAYOUT
    return IssueCategory.OTHER


def _amount(text: str) -> Decimal | None:
    matches = _RUPEES.findall(text) or _SHORT.findall(text)
    if not matches:
        return None
    return Decimal(matches[-1].replace(",", ""))


def _date(text: str, received_at: datetime) -> date | None:
    match = _ISO_DATE.search(text)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    today = received_at.date()
    if re.search(r"\b(?:kal|yesterday)\b|कल", text, re.IGNORECASE):
        return today - timedelta(days=1)
    if re.search(r"\b(?:aaj|today)\b|आज", text, re.IGNORECASE):
        return today
    match = _DAY.search(text)
    if match:
        day = int(match.group(1))
        try:
            candidate = today.replace(day=day)
        except ValueError:
            return None
        if candidate > today:
            previous_month = today.replace(day=1) - timedelta(days=1)
            try:
                candidate = previous_month.replace(day=day)
            except ValueError:
                return None
        return candidate
    for number, weekday in enumerate(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    ):
        if re.search(rf"\b{weekday}\b", text, re.IGNORECASE):
            days_ago = (today.weekday() - number) % 7 or 7
            return today - timedelta(days=days_ago)
    return None


class _OfflineSession:
    def __init__(self, extraction: ComplaintExtraction) -> None:
        self.extraction = extraction
        self.called = False

    async def next_action(self, tool_results: list[ToolResult] | None = None) -> ModelAction:
        if self.called:
            return ModelAction()
        self.called = True
        calls: list[ToolCall] = []
        for issue in self.extraction.issues:
            if issue.trip_id:
                calls.append(
                    ToolCall(name="get_trip_details", arguments={"trip_id": issue.trip_id})
                )
            elif issue.earning_date:
                calls.append(
                    ToolCall(
                        name="get_rider_trips",
                        arguments={"earning_date": issue.earning_date.isoformat()},
                    )
                )
            calls.extend(
                [
                    ToolCall(
                        name="get_payout_history",
                        arguments={"earning_date": issue.earning_date.isoformat()}
                        if issue.earning_date
                        else {},
                    ),
                    ToolCall(
                        name="get_provider_transactions",
                        arguments={"earning_date": issue.earning_date.isoformat()}
                        if issue.earning_date
                        else {},
                    ),
                    ToolCall(
                        name="get_applicable_policy",
                        arguments={"earning_date": issue.earning_date.isoformat()}
                        if issue.earning_date
                        else {},
                    ),
                    ToolCall(
                        name="calculate_verified_outstanding",
                        arguments={
                            "category": issue.category.value,
                            **({"trip_id": issue.trip_id} if issue.trip_id else {}),
                            **(
                                {"earning_date": issue.earning_date.isoformat()}
                                if issue.earning_date
                                else {}
                            ),
                        },
                    ),
                ]
            )
        if self.extraction.status_request:
            calls.append(ToolCall(name="get_dispute_history", arguments={}))
        unique: dict[tuple[str, str], ToolCall] = {}
        for call in calls:
            unique[(call.name, str(sorted(call.arguments.items())))] = call
        return ModelAction(tool_calls=list(unique.values()))


class DeterministicAgentClient:
    async def extract(
        self, text: str, state: ConversationState, received_at: datetime
    ) -> ComplaintExtraction:
        correction = bool(
            _CORRECTION_MARKER.search(text)
            or _VALUE_CORRECTION.search(text)
            or _TRIP_CORRECTION.search(text)
        )
        status_request = bool(_STATUS.search(text))
        segments = (
            [text]
            if correction
            else re.split(r"\s+(?:aur|and|और)\s+|[;,]", text, flags=re.IGNORECASE)
        )
        issues: list[ExtractedIssue] = []
        for segment in segments[:8]:
            segment = segment.strip()
            if not segment or (not correction and not _COMPLAINT.search(segment)):
                continue
            category = _category(segment)
            trip_ids = [match.upper() for match in _TRIP.findall(segment)] or [None]
            amount = _amount(segment)
            earning_date = _date(segment, received_at)
            if category == IssueCategory.OTHER and amount is None and not correction:
                continue
            for trip_id in trip_ids:
                confidence = (
                    0.9
                    if category != IssueCategory.OTHER
                    or (
                        correction
                        and len(state.issues) == 1
                        and (amount is not None or trip_id is not None)
                    )
                    else 0.6
                )
                issues.append(
                    ExtractedIssue(
                        category=category,
                        trip_id=trip_id,
                        earning_date=earning_date,
                        claimed_amount=amount,
                        correction=correction,
                        confidence=confidence,
                        clarification_needed=category == IssueCategory.OTHER and not state.issues,
                    )
                )
        question = None
        if not issues and not status_request:
            question = "Which trip or earning date should I check?"
        elif (
            correction
            and len(state.issues) > 1
            and issues
            and all(
                not issue.trip_id
                and not issue.earning_date
                and issue.category == IssueCategory.OTHER
                for issue in issues
            )
        ):
            question = "Which of your complaints are you correcting?"
        return ComplaintExtraction(
            language=_language(text),
            issues=issues,
            status_request=status_request,
            clarification_question=question,
        )

    def start_investigation(
        self, text: str, state: ConversationState, extraction: ComplaintExtraction
    ) -> InvestigationSession:
        return _OfflineSession(extraction)

    async def compose_reply(
        self,
        text: str,
        state: ConversationState,
        extraction: ComplaintExtraction,
        evidence: list[ToolResult],
    ) -> str:
        if extraction.clarification_question:
            return extraction.clarification_question
        if not extraction.issues and extraction.status_request:
            return "I’m checking the status of your existing complaints."
        labels = [
            issue.category.value.replace("_", " ")
            + (f" for trip {issue.trip_id}" if issue.trip_id else "")
            for issue in extraction.issues
        ]
        if extraction.language in {"hindi", "hinglish", "mixed"}:
            return (
                "Maine "
                + ", ".join(labels)
                + " ki complaint note kar li hai. Records check ho rahe hain; payment status confirm hone par hi bataya jayega."
            )
        return (
            "I’ve recorded your "
            + ", ".join(labels)
            + " complaint. The records are being checked; payment status will be confirmed separately."
        )
