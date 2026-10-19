from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agent import DeterministicAgentClient, InMemoryMemoryStore, QuickDropAgent, ToolExecutor
from app.agent.schemas import IssueCategory, ToolCall

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def make_agent():
    calls = []

    async def handler(rider_id, args):
        calls.append((rider_id, args))
        return {"source": "synthetic", "checked": True}

    names = (
        "get_rider_trips",
        "get_trip_details",
        "get_payout_history",
        "get_provider_transactions",
        "get_applicable_policy",
        "calculate_verified_outstanding",
        "get_dispute_history",
    )
    memory = InMemoryMemoryStore()
    agent = QuickDropAgent(
        DeterministicAgentClient(), ToolExecutor({name: handler for name in names}), memory
    )
    return agent, memory, calls


@pytest.mark.asyncio
async def test_hinglish_two_issues_and_authenticated_identity():
    agent, memory, calls = make_agent()
    result = await agent.handle_message(
        "R003", "C003", "M001", "Order T100 ka surge aur T200 ka incentive missing hai", NOW
    )
    assert len(result.issues) == 2
    assert {issue.category for issue in result.issues} == {
        IssueCategory.SURGE,
        IssueCategory.INCENTIVE,
    }
    assert {issue.trip_id for issue in result.issues} == {"T100", "T200"}
    assert result.extraction.language == "hinglish"
    assert all(rider_id == "R003" for rider_id, _ in calls)
    assert all("rider_id" not in item.arguments for item in result.tool_trace)
    assert len((await memory.load("R003", "C003")).issues) == 2


@pytest.mark.asyncio
async def test_nahi_mila_is_missing_surge_not_a_correction():
    agent, _, _ = make_agent()
    result = await agent.handle_message(
        "R003", "C003", "M001", "Bhai order T0001201 ka surge nahi mila", NOW
    )
    assert not result.clarification_needed
    assert not result.extraction.issues[0].correction
    assert len(result.issues) == 1
    assert result.issues[0].category == IssueCategory.SURGE
    assert result.issues[0].trip_id == "T0001201"
    assert any(item.name == "get_trip_details" for item in result.tool_trace)


@pytest.mark.asyncio
async def test_correction_preserves_original_and_duplicate_is_idempotent():
    agent, memory, calls = make_agent()
    first = await agent.handle_message(
        "R003", "C003", "M001", "Bhai kal ka payout 120 kam aaya", NOW
    )
    second = await agent.handle_message(
        "R003", "C003", "M002", "Actually 120 nahi, 150 kam hai", NOW
    )
    assert len(second.issues) == 1
    issue = second.issues[0]
    assert issue.original_claimed_amount == Decimal(120)
    assert issue.claimed_amount == Decimal(150)
    assert issue.category == IssueCategory.TOTAL_PAYOUT
    assert len(issue.revisions) == 1
    assert issue.revisions[0].previous["claimed_amount"] == Decimal(120)
    call_count = len(calls)
    replay = await agent.handle_message(
        "R003", "C003", "M002", "Actually 120 nahi, 150 kam hai", NOW
    )
    assert replay.duplicate
    assert replay.reply == second.reply
    assert len(calls) == call_count
    assert len((await memory.load("R003", "C003")).issues) == 1
    assert first.issues[0].claimed_amount == Decimal(120)


@pytest.mark.asyncio
async def test_ambiguous_correction_does_not_change_two_existing_claims():
    agent, _, _ = make_agent()
    first = await agent.handle_message(
        "R003", "C003", "M001", "Order T100 ka surge aur T200 ka incentive missing hai", NOW
    )
    result = await agent.handle_message("R003", "C003", "M002", "Sorry ₹120 nahi, ₹150", NOW)
    assert result.clarification_needed
    assert result.requires_review
    assert {issue.issue_id for issue in result.issues} == {issue.issue_id for issue in first.issues}
    assert all(issue.claimed_amount is None for issue in result.issues)


@pytest.mark.asyncio
async def test_tool_rejects_spoofed_identity_and_write_method():
    async def handler(rider_id, args):
        return {"rider": rider_id}

    executor = ToolExecutor({"get_trip_details": handler})
    forged = await executor.execute(
        "R003", ToolCall(name="get_trip_details", arguments={"trip_id": "T100", "rider_id": "R999"})
    )
    assert not forged.success and forged.result["error"] == "invalid_arguments"
    write = await executor.execute(
        "R003", ToolCall(name="execute_payment", arguments={"amount": 200})
    )
    assert not write.success and write.result["error"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_status_request_uses_dispute_history_without_new_issue():
    agent, _, _ = make_agent()
    result = await agent.handle_message(
        "R003", "C003", "M001", "Jo complaint tha uska kya hua?", NOW
    )
    assert result.extraction.status_request
    assert not result.issues
    assert any(item.name == "get_dispute_history" for item in result.tool_trace)


@pytest.mark.asyncio
async def test_nonsettlement_reply_from_offline_agent():
    agent, _, _ = make_agent()
    result = await agent.handle_message("R003", "C003", "M001", "T100 surge missing", NOW)
    assert "payment sent" not in result.reply.lower()
    assert "paid" not in result.reply.lower()


@pytest.mark.asyncio
async def test_corrected_trip_identifier_revises_one_existing_claim():
    agent, memory, _ = make_agent()
    first = await agent.handle_message("R003", "C003", "M001", "Trip T0001202 surge missing", NOW)
    corrected = await agent.handle_message("R003", "C003", "M002", "Sorry, I meant T0001201", NOW)
    assert len(first.issues) == len(corrected.issues) == 1
    assert corrected.issues[0].issue_id == first.issues[0].issue_id
    assert corrected.issues[0].trip_id == "T0001201"
    assert not corrected.requires_review
    assert corrected.issues[0].revisions[0].previous["trip_id"] == "T0001202"
    assert (await memory.load("R003", "C003")).issues[0].trip_id == "T0001201"


@pytest.mark.asyncio
async def test_model_outage_defers_without_creating_a_payment_or_false_confirmation():
    agent, memory, calls = make_agent()

    class FailingModel:
        async def extract(self, *_args):
            raise ConnectionError("Gemini unavailable")

    agent.client = FailingModel()
    result = await agent.handle_message("R003", "C003", "M001", "My payout is short by Rs 500", NOW)
    assert result.deferred and result.requires_review
    assert not result.issues and not calls
    assert "paid" not in result.reply.lower()
    assert "M001" in (await memory.load("R003", "C003")).processed_message_ids
