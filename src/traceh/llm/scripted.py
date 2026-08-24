"""Deterministic provider for tests, demos and protocol traces."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from traceh.api.llm import ModelRequest, ModelResponse, ToolCall, Usage, UsageQuality


class ScriptExhaustedError(RuntimeError):
    pass


class ScriptedLlmProvider:
    name = "scripted"

    def __init__(
        self,
        responses: Sequence[ModelResponse],
        *,
        delay_seconds: float = 0.0,
        repeat_last: bool = False,
    ) -> None:
        self._responses = tuple(responses)
        self._index = 0
        self.delay_seconds = delay_seconds
        self.repeat_last = repeat_last
        self.requests: list[ModelRequest] = []

    @classmethod
    def from_file(cls, path: Path) -> "ScriptedLlmProvider":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("script file must contain a JSON array")
        responses = tuple(cls._parse_response(item) for item in raw)
        return cls(responses)

    @staticmethod
    def _parse_response(raw: object) -> ModelResponse:
        if not isinstance(raw, Mapping):
            raise ValueError("each scripted response must be a JSON object")
        raw_calls = raw.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be a list")
        calls = []
        for item in raw_calls:
            if not isinstance(item, Mapping):
                raise ValueError("tool call must be an object")
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("tool call arguments must be an object")
            calls.append(
                ToolCall(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    arguments={str(k): v for k, v in arguments.items()},
                )
            )
        usage_raw = raw.get("usage", {})
        if not isinstance(usage_raw, Mapping):
            usage_raw = {}
        has_exact_usage = (
            type(usage_raw.get("input_tokens")) is int
            and usage_raw["input_tokens"] >= 0
            and type(usage_raw.get("output_tokens")) is int
            and usage_raw["output_tokens"] >= 0
        )
        return ModelResponse(
            content=str(raw.get("content") or ""),
            tool_calls=tuple(calls),
            finish_reason=str(raw.get("finish_reason") or ("tool_calls" if calls else "stop")),
            usage=Usage(
                input_tokens=int(usage_raw.get("input_tokens", 0)),
                output_tokens=int(usage_raw.get("output_tokens", 0)),
                quality=(
                    UsageQuality.EXACT if has_exact_usage else UsageQuality.UNKNOWN
                ),
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self._responses:
            return ModelResponse(content="Scripted provider has no configured response.")
        if self._index >= len(self._responses):
            if self.repeat_last:
                return self._responses[-1]
            raise ScriptExhaustedError(
                f"script exhausted after {len(self._responses)} model request(s)"
            )
        response = self._responses[self._index]
        self._index += 1
        return response
