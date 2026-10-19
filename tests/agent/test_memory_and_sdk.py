import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import types

from app.agent.gemini_client import EXTRACTION_RESPONSE_SCHEMA, GeminiClient
from app.agent.memory import SqlAlchemyMemoryStore
from app.agent.prompts import REPLY_INSTRUCTION
from app.agent.schemas import ComplaintExtraction, ConversationState, IssueCategory, ToolResult
from app.agent.tool_registry import gemini_tool_declarations


def test_installed_google_genai_manual_call_types():
    tools = gemini_tool_declarations()
    assert len(tools) == 1
    assert len(tools[0].function_declarations) == 7
    names = {item.name for item in tools[0].function_declarations}
    assert "execute_payment" not in names
    config = types.GenerateContentConfig(
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    assert config.automatic_function_calling.disable is True
    assert types.Part.from_function_response(name="get_trip_details", response={"ok": True})
    structured = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=EXTRACTION_RESPONSE_SCHEMA
    )
    assert structured.response_schema == EXTRACTION_RESPONSE_SCHEMA
    assert "additionalProperties" not in str(structured.response_schema)


@pytest.mark.asyncio
async def test_gemini_extraction_uses_api_safe_schema_and_validates_result():
    class FakeModels:
        def __init__(self):
            self.config = None

        async def generate_content(self, **kwargs):
            self.config = kwargs["config"]
            return SimpleNamespace(
                parsed={
                    "language": "en",
                    "issues": [{"category": "surge", "trip_id": "t100", "claimed_amount": "12.50"}],
                    "status_request": False,
                },
                text=None,
            )

    models = FakeModels()
    client = object.__new__(GeminiClient)
    client.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    client.types = types
    client.model = "gemini-2.5-flash"
    state = ConversationState(rider_id="R003", conversation_id="C003")
    extraction = await client.extract("T100 surge missing", state, datetime.now(UTC))

    assert extraction.issues[0].trip_id == "T100"
    assert extraction.issues[0].claimed_amount == Decimal("12.50")
    assert models.config.response_schema == EXTRACTION_RESPONSE_SCHEMA
    with pytest.raises(ValueError):
        ComplaintExtraction.model_validate({"issues": [{"category": "surge", "extra": True}]})


@pytest.mark.asyncio
async def test_gemini_session_returns_tool_evidence_through_sdk_parts():
    class FakeModels:
        def __init__(self):
            self.calls = []

        async def generate_content(self, **kwargs):
            self.calls.append({**kwargs, "contents": list(kwargs["contents"])})
            if len(self.calls) == 1:
                part = types.Part.from_function_call(
                    name="get_trip_details", args={"trip_id": "T100"}
                )
            else:
                part = types.Part.from_text(text="Evidence received")
            return types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
            )

    models = FakeModels()
    client = object.__new__(GeminiClient)
    client.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    client.types = types
    client.model = "gemini-2.5-flash"
    state = ConversationState(rider_id="R003", conversation_id="C003")
    session = client.start_investigation("T100 surge missing", state, ComplaintExtraction())
    action = await session.next_action()
    assert action.tool_calls[0].name == "get_trip_details"
    assert models.calls[0]["config"].automatic_function_calling.disable is True
    done = await session.next_action(
        [
            ToolResult(
                name="get_trip_details",
                arguments={"trip_id": "T100"},
                result={"found": True},
                success=True,
            )
        ]
    )
    assert not done.tool_calls
    assert models.calls[1]["contents"][-1].role == "tool"
    assert models.calls[1]["contents"][-1].parts[0].function_response.name == "get_trip_details"


@pytest.mark.asyncio
async def test_gemini_compose_reply_uses_async_sdk_and_sends_evidence():
    class FakeModels:
        def __init__(self):
            self.calls = []

        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(text="  Checking your payout.  ")

    models = FakeModels()
    client = object.__new__(GeminiClient)
    client.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    client.types = types
    client.model = "gemini-2.5-flash"
    state = ConversationState(rider_id="R003", conversation_id="C003")
    evidence = [
        ToolResult(
            name="get_trip_details",
            arguments={"trip_id": "T100"},
            result={"found": True},
            success=True,
        )
    ]

    reply = await client.compose_reply(
        "T100 payout missing", state, ComplaintExtraction(), evidence
    )

    assert reply == "Checking your payout."
    assert len(models.calls) == 1
    request = models.calls[0]
    assert request["model"] == "gemini-2.5-flash"
    assert request["config"].system_instruction == REPLY_INSTRUCTION
    assert request["config"].temperature == 0.2
    payload = json.loads(request["contents"])
    assert payload["latest_rider_message"] == "T100 payout missing"
    assert payload["issue_identifiers"] == []
    assert payload["tool_evidence"] == [evidence[0].model_dump(mode="json")]


@pytest.mark.asyncio
async def test_sqlalchemy_adapter_reconstructs_committed_conversation(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-32-characters-long-001")
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-webhook")
    monkeypatch.setenv("MOCKPAYSWIFT_API_KEY", "test-provider")
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import Conversation, Dispute, DisputeRevision, Message, Rider

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            Rider(
                id="R003",
                display_name="Synthetic Rider",
                phone_identifier="demo:R003",
                city="Fiction City",
                status="ACTIVE",
            )
        )
        session.add(Conversation(id="C003", rider_id="R003"))
        await session.flush()
        session.add_all(
            [
                Message(
                    message_id="M001",
                    rider_id="R003",
                    conversation_id="C003",
                    sender="rider",
                    text="₹120 kam",
                    reply="Checking",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                ),
                Message(
                    message_id="M002",
                    rider_id="R003",
                    conversation_id="C003",
                    sender="rider",
                    text="Actually ₹150",
                    reply="Checking again",
                    received_at=datetime.now(UTC),
                    status="PROCESSED",
                ),
                Message(
                    message_id="M003",
                    rider_id="R003",
                    conversation_id="C003",
                    sender="rider",
                    text="New pending message",
                    reply=None,
                    received_at=datetime.now(UTC),
                    status="RUNNING",
                ),
            ]
        )
        await session.flush()
        session.add(
            Dispute(
                id="D001",
                rider_id="R003",
                conversation_id="C003",
                source_message_id="M001",
                category="TOTAL_PAYOUT",
                claimed_amount=Decimal("150.00"),
                status="OPS_REVIEW",
                revision=2,
            )
        )
        await session.flush()
        session.add_all(
            [
                DisputeRevision(
                    dispute_id="D001",
                    message_id="M001",
                    revision=1,
                    claimed_amount=Decimal("120.00"),
                    trip_id=None,
                    category="TOTAL_PAYOUT",
                ),
                DisputeRevision(
                    dispute_id="D001",
                    message_id="M002",
                    revision=2,
                    claimed_amount=Decimal("150.00"),
                    trip_id=None,
                    category="TOTAL_PAYOUT",
                ),
            ]
        )
        await session.commit()

    store = SqlAlchemyMemoryStore(sessions)
    state = await store.load("R003", "C003")
    assert state.processed_message_ids == ["M001", "M002"]
    assert "M003" not in state.processed_message_ids
    assert state.previous_reply_by_message["M002"] == "Checking again"
    assert len(state.issues) == 1
    issue = state.issues[0]
    assert issue.category == IssueCategory.TOTAL_PAYOUT
    assert issue.original_claimed_amount == Decimal("120.00")
    assert issue.claimed_amount == Decimal("150.00")
    assert issue.requires_review
    assert issue.source_message_ids == ["M001", "M002"]
    assert len(issue.revisions) == 1
    await store.save(state)
    async with sessions() as session:
        from app.agent import SqlMemoryStore

        shared_session_state = await SqlMemoryStore(session).load("R003", "C003")
        assert shared_session_state.issues[0].issue_id == "D001"
        assert len((await session.scalars(select(Dispute))).all()) == 1
    await engine.dispose()
