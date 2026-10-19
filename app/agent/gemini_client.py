"""Official google-genai client with explicit, manual function calling."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from .prompts import EXTRACTION_INSTRUCTION, INVESTIGATION_INSTRUCTION, REPLY_INSTRUCTION
from .schemas import ComplaintExtraction, ConversationState, IssueCategory, ToolCall, ToolResult
from .tool_registry import gemini_tool_declarations


class ModelAction(BaseModel):
    tool_calls: list[ToolCall] = Field(default_factory=list)
    text: str = ""


# The Gemini response_schema endpoint does not accept the additionalProperties
# keywords emitted by StrictModel. Keep its schema small and validate the result
# against ComplaintExtraction after the SDK returns it.
EXTRACTION_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "language": {"type": "STRING"},
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "category": {
                        "type": "STRING",
                        "enum": [category.value for category in IssueCategory],
                    },
                    "trip_id": {"type": "STRING"},
                    "earning_date": {
                        "type": "STRING",
                        "description": "Date in YYYY-MM-DD format",
                    },
                    "claimed_amount": {
                        "type": "STRING",
                        "description": "Decimal amount in rupees, e.g. 123.45",
                    },
                    "correction": {"type": "BOOLEAN"},
                    "target_issue_id": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "clarification_needed": {"type": "BOOLEAN"},
                },
                "required": ["category"],
            },
        },
        "status_request": {"type": "BOOLEAN"},
        "clarification_question": {"type": "STRING"},
    },
    "required": ["issues"],
}


class InvestigationSession(Protocol):
    async def next_action(self, tool_results: list[ToolResult] | None = None) -> ModelAction: ...


class AgentClient(Protocol):
    async def extract(
        self, text: str, state: ConversationState, received_at: datetime
    ) -> ComplaintExtraction: ...

    def start_investigation(
        self, text: str, state: ConversationState, extraction: ComplaintExtraction
    ) -> InvestigationSession: ...

    async def compose_reply(
        self,
        text: str,
        state: ConversationState,
        extraction: ComplaintExtraction,
        evidence: list[ToolResult],
    ) -> str: ...


class _GeminiInvestigationSession:
    def __init__(self, client: GeminiClient, prompt: str) -> None:
        self.client = client
        self.contents: list[object] = [
            client.types.Content(role="user", parts=[client.types.Part.from_text(text=prompt)])
        ]
        self.last_calls: list[ToolCall] = []

    async def next_action(self, tool_results: list[ToolResult] | None = None) -> ModelAction:
        types = self.client.types
        if tool_results is not None:
            if len(tool_results) != len(self.last_calls):
                raise ValueError("tool response count does not match pending calls")
            self.contents.append(
                types.Content(
                    role="tool",
                    parts=[
                        types.Part.from_function_response(name=item.name, response=item.result)
                        for item in tool_results
                    ],
                )
            )
        response = await self.client.client.aio.models.generate_content(
            model=self.client.model,
            contents=self.contents,
            config=types.GenerateContentConfig(
                system_instruction=INVESTIGATION_INSTRUCTION,
                tools=gemini_tool_declarations(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0,
            ),
        )
        if not response.candidates or response.candidates[0].content is None:
            raise ValueError("Gemini returned no investigation content")
        self.contents.append(response.candidates[0].content)
        self.last_calls = [
            ToolCall(name=call.name or "", arguments=dict(call.args or {}))
            for call in (response.function_calls or [])
        ]
        return ModelAction(
            tool_calls=self.last_calls,
            text="" if self.last_calls else (response.text or ""),
        )


class GeminiClient:
    """One Gemini model handles extraction, investigation, and rider wording."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        if not api_key:
            raise ValueError("Gemini API key is required")
        from google import genai
        from google.genai import types

        self.client = genai.Client(api_key=api_key)
        self.types = types
        self.model = model

    async def extract(
        self, text: str, state: ConversationState, received_at: datetime
    ) -> ComplaintExtraction:
        context = {
            "message_time_asia_kolkata": received_at.isoformat(),
            "latest_rider_message": text,
            "existing_issues": [issue.model_dump(mode="json") for issue in state.issues[-8:]],
        }
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=json.dumps(context, ensure_ascii=False),
            config=self.types.GenerateContentConfig(
                system_instruction=EXTRACTION_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=EXTRACTION_RESPONSE_SCHEMA,
                temperature=0,
            ),
        )
        payload = response.parsed if response.parsed is not None else response.text
        if payload is None:
            raise ValueError("Gemini returned no structured extraction")
        return (
            ComplaintExtraction.model_validate_json(payload)
            if isinstance(payload, str)
            else ComplaintExtraction.model_validate(payload)
        )

    def start_investigation(
        self, text: str, state: ConversationState, extraction: ComplaintExtraction
    ) -> InvestigationSession:
        prompt = json.dumps(
            {
                "latest_rider_message": text,
                "existing_issues": [issue.model_dump(mode="json") for issue in state.issues[-8:]],
                "extraction": extraction.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        return _GeminiInvestigationSession(self, prompt)

    async def compose_reply(
        self,
        text: str,
        state: ConversationState,
        extraction: ComplaintExtraction,
        evidence: list[ToolResult],
    ) -> str:
        payload = {
            "latest_rider_message": text,
            "extraction": extraction.model_dump(mode="json"),
            "issue_identifiers": [issue.issue_id for issue in state.issues[-8:]],
            "tool_evidence": [item.model_dump(mode="json") for item in evidence[-12:]],
            "trusted_payment_status": "No payment decision or settlement has been supplied yet.",
        }
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=json.dumps(payload, ensure_ascii=False),
            config=self.types.GenerateContentConfig(
                system_instruction=REPLY_INSTRUCTION,
                temperature=0.2,
            ),
        )
        reply = (response.text or "").strip()
        if not reply:
            raise ValueError("Gemini returned no rider reply")
        return reply[:1000]
