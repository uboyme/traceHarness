"""Dependency-free OpenAI-compatible chat-completions provider."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from traceh.api.llm import ModelRequest, ModelResponse, ToolCall, Usage, UsageQuality
from traceh.concurrency import await_worker_convergence


class ProviderHttpError(RuntimeError):
    pass


@dataclass(slots=True)
class OpenAICompatibleProvider:
    base_url: str
    api_key: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 120.0
    name: str = "openai-compatible"

    def _key(self) -> str | None:
        return self.api_key or os.environ.get(self.api_key_env)

    @staticmethod
    def _message_payload(request: ModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for message in request.messages:
            payload: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_call_id:
                payload["tool_call_id"] = message.tool_call_id
            if message.name:
                payload["name"] = message.name
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(payload)
        return messages

    def _complete_sync(self, request: ModelRequest) -> ModelResponse:
        endpoint = self.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._message_payload(request),
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        http_request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise ProviderHttpError(f"HTTP {error.code}: {body[:4000]}") from error
        except urllib.error.URLError as error:
            raise ProviderHttpError(str(error)) from error

        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ProviderHttpError("provider response contains no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderHttpError("provider choice is not an object")
        message = choice.get("message", {})
        if not isinstance(message, dict):
            raise ProviderHttpError("provider message is not an object")
        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls", [])
        if isinstance(raw_calls, list):
            for item in raw_calls:
                if not isinstance(item, dict):
                    continue
                function = item.get("function", {})
                if not isinstance(function, dict):
                    continue
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError:
                    arguments = {"_raw": str(raw_arguments)}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("id") or f"call-{len(tool_calls) + 1}"),
                        name=str(function.get("name") or ""),
                        arguments={str(k): v for k, v in arguments.items()},
                    )
                )
        usage_raw = raw.get("usage", {}) if isinstance(raw, dict) else {}
        if not isinstance(usage_raw, dict):
            usage_raw = {}
        usage_is_exact = (
            type(usage_raw.get("prompt_tokens")) is int
            and usage_raw["prompt_tokens"] >= 0
            and type(usage_raw.get("completion_tokens")) is int
            and usage_raw["completion_tokens"] >= 0
        )
        return ModelResponse(
            content=str(message.get("content") or ""),
            tool_calls=tuple(tool_calls),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0)),
                output_tokens=int(usage_raw.get("completion_tokens", 0)),
                quality=(
                    UsageQuality.EXACT if usage_is_exact else UsageQuality.UNKNOWN
                ),
            ),
            raw={"response_id": str(raw.get("id", ""))} if isinstance(raw, dict) else {},
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run the blocking HTTP call on a worker thread.

        Cancellation cannot abort a `urllib` request that is already in flight,
        and a detached worker would keep the socket open and keep writing into
        the runtime after the caller was told the turn had ended. So the worker
        is shielded, waited for, and only then is the original cancellation
        re-raised. In the worst case that wait lasts until `timeout_seconds`
        expires: this is convergence, not an immediate abort of the request.
        """

        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, self._complete_sync, request)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            await await_worker_convergence(worker)
            raise cancellation
