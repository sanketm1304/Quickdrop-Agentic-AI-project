"""Server-side allowlist, validation, identity injection, and timeouts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import ToolCall, ToolResult
from .tool_registry import TOOL_SCHEMAS

ReadOnlyHandler = Callable[[str, BaseModel], Awaitable[Mapping[str, Any]]]


class ToolExecutor:
    """Callbacks must enforce rider ownership for every database/provider read."""

    def __init__(
        self, handlers: Mapping[str, ReadOnlyHandler], timeout_seconds: float = 5.0
    ) -> None:
        unknown = set(handlers) - set(TOOL_SCHEMAS)
        if unknown:
            raise ValueError(f"non-allowlisted tool handlers: {sorted(unknown)}")
        self.handlers = dict(handlers)
        self.timeout_seconds = timeout_seconds

    async def execute(self, rider_id: str, call: ToolCall) -> ToolResult:
        schema = TOOL_SCHEMAS.get(call.name)
        handler = self.handlers.get(call.name)
        if schema is None or handler is None:
            return ToolResult(
                name=call.name, arguments={}, result={"error": "tool_not_allowed"}, success=False
            )
        try:
            arguments = schema.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(
                name=call.name, arguments={}, result={"error": "invalid_arguments"}, success=False
            )
        validated = arguments.model_dump(mode="json", exclude_none=True)
        try:
            response = await asyncio.wait_for(
                handler(rider_id, arguments), timeout=self.timeout_seconds
            )
            if not isinstance(response, Mapping):
                raise TypeError("tool response must be a mapping")
            normalized = json.loads(json.dumps(dict(response), default=str, ensure_ascii=False))
            if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > 16_384:
                return ToolResult(
                    name=call.name,
                    arguments=validated,
                    result={"error": "result_too_large"},
                    success=False,
                )
            return ToolResult(name=call.name, arguments=validated, result=normalized, success=True)
        except TimeoutError:
            return ToolResult(
                name=call.name, arguments=validated, result={"error": "tool_timeout"}, success=False
            )
        except Exception:  # noqa: BLE001 - never leak provider or database errors to the model
            # Do not leak database details or provider credentials into model context.
            return ToolResult(
                name=call.name,
                arguments=validated,
                result={"error": "tool_unavailable"},
                success=False,
            )
