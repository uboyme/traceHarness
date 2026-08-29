"""Model-neutral request and response protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from traceh.api.json_types import JsonValue, fingerprint, to_json_value

REQUEST_SNAPSHOT_KEYS = frozenset(
    {
        "turn_id",
        "step_id",
        "source_seq",
        "composition_revision",
        "composed_fingerprint",
        "dispatch_fingerprint",
        "composed_request",
        "dispatch_request",
    }
)


def _require_attempt_identity(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ModelAttemptIdentity:
    """Host-owned identity for one provider dispatch authorization.

    The identity is deliberately outside :class:`ModelRequest.metadata`: an
    Attempt and its Budget reservation must not change either the composed or
    provider-bound request fingerprint.
    """

    session_id: str
    turn_id: str
    step_id: str
    attempt_id: str
    ordinal: int

    def __post_init__(self) -> None:
        _require_attempt_identity(self.session_id, field_name="session_id")
        _require_attempt_identity(self.turn_id, field_name="turn_id")
        _require_attempt_identity(self.step_id, field_name="step_id")
        _require_attempt_identity(self.attempt_id, field_name="attempt_id")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("ordinal must be a positive integer")


def model_attempt_reservation_id(identity: ModelAttemptIdentity) -> str:
    """Bind one Budget hold to one exact Attempt without request metadata."""

    if type(identity) is not ModelAttemptIdentity:
        raise TypeError("identity must be ModelAttemptIdentity")
    return fingerprint(
        {
            "kind": "model-attempt-reservation",
            "identity": {
                "session_id": identity.session_id,
                "turn_id": identity.turn_id,
                "step_id": identity.step_id,
                "attempt_id": identity.attempt_id,
                "ordinal": identity.ordinal,
            },
        }
    )


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ToolCall:
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("tool call arguments must be an object")
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            arguments={str(k): to_json_value(v) for k, v in arguments.items()},
        )


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    name: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.name is not None:
            result["name"] = self.name
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ModelMessage:
        raw_calls = raw.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ValueError("message.tool_calls must be a list")
        return cls(
            role=str(raw["role"]),
            content=str(raw.get("content") or ""),
            tool_call_id=(str(raw["tool_call_id"]) if raw.get("tool_call_id") else None),
            tool_calls=tuple(
                ToolCall.from_dict(item)
                for item in raw_calls
                if isinstance(item, dict)
            ),
            name=str(raw["name"]) if raw.get("name") else None,
        )


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ToolSchema:
        schema = raw.get("input_schema", {})
        if not isinstance(schema, dict):
            raise ValueError("tool schema input_schema must be an object")
        return cls(
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            input_schema={str(k): to_json_value(v) for k, v in schema.items()},
        )


class UsageQuality(StrEnum):
    """How trustworthy a provider's token count is.

    Missing usage is not zero usage.  Budget enforcement may accept an
    estimate only when the host explicitly opts into that weaker contract;
    otherwise an unknown response consumes the whole reservation.
    """

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    quality: UsageQuality = UsageQuality.UNKNOWN

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "quality": self.quality.value,
        }


@dataclass(frozen=True, slots=True)
class ModelRequest:
    provider: str
    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    system_prompt: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "messages": [message.to_dict() for message in self.messages],
            "tools": [tool.to_dict() for tool in self.tools],
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ModelRequest:
        raw_messages = raw.get("messages", [])
        raw_tools = raw.get("tools", [])
        if not isinstance(raw_messages, list) or not isinstance(raw_tools, list):
            raise ValueError("request messages and tools must be lists")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            system_prompt=(str(raw["system_prompt"]) if raw.get("system_prompt") else None),
            messages=tuple(
                ModelMessage.from_dict(item) for item in raw_messages if isinstance(item, dict)
            ),
            tools=tuple(ToolSchema.from_dict(item) for item in raw_tools if isinstance(item, dict)),
            temperature=float(raw["temperature"]) if raw.get("temperature") is not None else None,
            max_output_tokens=(
                int(raw["max_output_tokens"])
                if raw.get("max_output_tokens") is not None
                else None
            ),
            metadata={str(k): to_json_value(v) for k, v in metadata.items()},
        )


def dispatch_request_matches_composed(
    composed: ModelRequest,
    dispatch: ModelRequest,
) -> bool:
    """Return whether admission only kept or lowered the output ceiling."""

    if type(composed) is not ModelRequest or type(dispatch) is not ModelRequest:
        return False
    composed_data = composed.to_dict()
    dispatch_data = dispatch.to_dict()
    composed_limit = composed_data.pop("max_output_tokens")
    dispatch_limit = dispatch_data.pop("max_output_tokens")
    if composed_data != dispatch_data:
        return False
    if composed_limit is None:
        return dispatch_limit is None or (
            type(dispatch_limit) is int and dispatch_limit > 0
        )
    return (
        type(composed_limit) is int
        and composed_limit > 0
        and type(dispatch_limit) is int
        and 0 < dispatch_limit <= composed_limit
    )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    raw: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict(),
            "raw": dict(self.raw),
        }


class LlmProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        ...
