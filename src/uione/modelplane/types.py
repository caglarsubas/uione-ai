"""Wire types for the model plane.

Deliberately a narrow subset of the OpenAI schema: only what UiOne actually sends
and reads. A narrow surface is what makes swapping serving runtimes cheap.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskClass(StrEnum):
    """What a call is *for*, which decides which model tier serves it.

    Routing by task class rather than by caller is what keeps GPU cost predictable:
    the overwhelming majority of traffic is triage and drafting, and only planning
    needs the expensive tier (gap G11).
    """

    TRIAGE = "triage"
    """Classification, routing, extraction, PII pre-screen. Smallest tier."""

    WORKHORSE = "workhorse"
    """Drafting, summarising, and most tool-calling. Where most tokens run."""

    REASONING = "reasoning"
    """Planning, multi-step agent loops, report synthesis. Most expensive tier."""


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolCall(BaseModel):
    """A tool invocation requested by the model.

    ``arguments`` stays a raw string because models emit malformed JSON often enough
    that parsing must be an explicit, recoverable step rather than a silent failure
    during deserialisation. The reliability layer owns repair (gap G5).
    """

    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        """Parse arguments, raising ``ValueError`` on malformed JSON."""
        try:
            parsed = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool call {self.name!r} produced invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"tool call {self.name!r} arguments must be an object, got {type(parsed).__name__}"
            )
        return parsed


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role}
        # Assistant turns that only request tools legitimately carry null content.
        msg["content"] = self.content if self.content is not None else ""
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.name:
            msg["name"] = self.name
        return msg


class ToolDefinition(BaseModel):
    """A tool offered to the model, in JSON Schema form."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Completion(BaseModel):
    """A single model response."""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    model: str = ""
    usage: Usage = Field(default_factory=Usage)

    @property
    def requested_tools(self) -> bool:
        return bool(self.tool_calls)
