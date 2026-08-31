"""Dependency-free OpenAI-compatible chat-completions provider."""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import os
import socket
import ssl
import tokenize
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from traceh.api.llm import ModelRequest, ModelResponse, ToolCall, Usage, UsageQuality
from traceh.concurrency import await_worker_convergence
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory

_HTTP_FAILURES: dict[int, tuple[str, ProviderFailureCategory]] = {
    400: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    401: ("provider-http-authentication", ProviderFailureCategory.AUTHENTICATION),
    403: ("provider-http-permission", ProviderFailureCategory.PERMISSION),
    404: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    405: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    408: ("provider-http-timeout", ProviderFailureCategory.TIMEOUT),
    409: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    413: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    414: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    415: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    422: ("provider-http-invalid-request", ProviderFailureCategory.INVALID_REQUEST),
    429: ("provider-http-rate-limited", ProviderFailureCategory.RATE_LIMITED),
    500: ("provider-http-server-transient", ProviderFailureCategory.SERVER_TRANSIENT),
    502: ("provider-http-server-transient", ProviderFailureCategory.SERVER_TRANSIENT),
    503: ("provider-http-server-transient", ProviderFailureCategory.SERVER_TRANSIENT),
    504: ("provider-http-server-transient", ProviderFailureCategory.SERVER_TRANSIENT),
}


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Accept only a finite non-negative delta-seconds value, never raw text."""

    value = error.headers.get("Retry-After") if error.headers is not None else None
    if not isinstance(value, str) or not value.isascii():
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        return None
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        return None
    return parsed


def _http_failure(error: urllib.error.HTTPError) -> ProviderFailure:
    code, category = _HTTP_FAILURES.get(
        error.code,
        ("provider-http-unknown", ProviderFailureCategory.UNKNOWN),
    )
    return ProviderFailure(
        code,
        category,
        retry_after_seconds=_retry_after_seconds(error),
    )


def _transport_failure(error: BaseException) -> ProviderFailure:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return ProviderFailure(
            "provider-dns-temporary",
            ProviderFailureCategory.DNS,
            usage=Usage(0, 0, UsageQuality.EXACT),
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT)
    if isinstance(reason, ssl.SSLEOFError):
        return ProviderFailure("provider-tls-eof", ProviderFailureCategory.TLS_EOF)
    if isinstance(
        reason,
        (
            ConnectionAbortedError,
            ConnectionResetError,
            BrokenPipeError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
        ),
    ):
        return ProviderFailure(
            "provider-disconnected",
            ProviderFailureCategory.DISCONNECTED,
        )
    return ProviderFailure("provider-transport-unknown", ProviderFailureCategory.UNKNOWN)


def _protocol_failure(
    code: str = "provider-response-invalid",
) -> ProviderFailure:
    return ProviderFailure(
        code,
        ProviderFailureCategory.PROTOCOL,
    )


def _reject_non_json_constant(value: str) -> None:
    del value
    raise ValueError("non-JSON numeric constant")


def _strict_json_loads(value: str | bytes) -> Any:
    """Parse RFC JSON without Python's NaN/Infinity extension."""

    return json.loads(value, parse_constant=_reject_non_json_constant)


def _line_offsets(source: str) -> tuple[int, ...]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return tuple(offsets)


def _source_offset(offsets: tuple[int, ...], position: tuple[int, int]) -> int:
    row, column = position
    if row < 1 or row > len(offsets):
        raise ValueError("token position is outside source")
    return offsets[row - 1] + column


def _declared_string_properties(
    schema: dict[str, Any] | None,
) -> frozenset[str]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return frozenset()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return frozenset()
    return frozenset(
        str(name)
        for name, definition in properties.items()
        if isinstance(definition, dict) and definition.get("type") == "string"
    )


def _normalize_triple_quoted_arguments(
    raw: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize one bounded provider dialect, then return strict JSON.

    Some OpenAI-compatible models emit a JSON object whose top-level string
    values use Python ``\"\"\"`` delimiters for multiline tool arguments. The
    rest of the object must remain strict JSON, and the affected property must
    be declared as a string by the exact Tool schema frozen in the request.
    This is deliberately not a general JSON repair or Python-literal parser.
    """

    allowed = _declared_string_properties(schema)
    if not allowed or '"""' not in raw:
        return None
    try:
        tokens = tuple(tokenize.generate_tokens(io.StringIO(raw).readline))
    except (IndentationError, tokenize.TokenError):
        return None

    offsets = _line_offsets(raw)
    stack: list[str] = []
    significant: list[tokenize.TokenInfo] = []
    replacements: list[tuple[int, int, str]] = []
    ignored = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    for token in tokens:
        if token.type in ignored:
            continue
        if token.type == tokenize.STRING and token.string.startswith('"""'):
            if stack != ["{"] or len(significant) < 2:
                return None
            key_token, colon_token = significant[-2:]
            if key_token.type != tokenize.STRING or colon_token.string != ":":
                return None
            if not token.string.endswith('"""') or len(token.string) < 6:
                return None
            try:
                key = _strict_json_loads(key_token.string)
                value = _strict_json_loads(f'"{token.string[3:-3]}"')
            except (json.JSONDecodeError, ValueError):
                return None
            if type(key) is not str or key not in allowed or type(value) is not str:
                return None
            try:
                start = _source_offset(offsets, token.start)
                end = _source_offset(offsets, token.end)
            except ValueError:
                return None
            replacements.append((start, end, json.dumps(value, ensure_ascii=False)))

        if token.type == tokenize.OP:
            if token.string in {"{", "["}:
                stack.append(token.string)
            elif token.string in {"}", "]"}:
                expected = "{" if token.string == "}" else "["
                if not stack or stack.pop() != expected:
                    return None
        significant.append(token)

    if not replacements or stack:
        return None
    normalized = raw
    for start, end, replacement in reversed(replacements):
        normalized = normalized[:start] + replacement + normalized[end:]
    try:
        parsed = _strict_json_loads(normalized)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_tool_arguments(
    raw: object,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise _protocol_failure("provider-tool-arguments-invalid")
    try:
        parsed = _strict_json_loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = _normalize_triple_quoted_arguments(raw, schema)
    if not isinstance(parsed, dict):
        raise _protocol_failure("provider-tool-arguments-invalid")
    return parsed


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
        try:
            http_request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
        except (TypeError, ValueError):
            raise ProviderFailure(
                "provider-configuration-invalid",
                ProviderFailureCategory.CONFIGURATION,
            ) from None
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise _http_failure(error) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            ssl.SSLEOFError,
            ConnectionAbortedError,
            ConnectionResetError,
            BrokenPipeError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
        ) as error:
            raise _transport_failure(error) from None
        except OSError:
            raise ProviderFailure(
                "provider-transport-unknown",
                ProviderFailureCategory.UNKNOWN,
            ) from None

        try:
            raw = _strict_json_loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _protocol_failure() from None

        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not isinstance(choices, list) or not choices:
            raise _protocol_failure() from None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise _protocol_failure() from None
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _protocol_failure() from None
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        if (content is not None and not isinstance(content, str)) or (
            finish_reason is not None and not isinstance(finish_reason, str)
        ):
            raise _protocol_failure() from None
        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise _protocol_failure() from None
        tool_schemas = {tool.name: tool.input_schema for tool in request.tools}
        for item in raw_calls:
            if not isinstance(item, dict):
                raise _protocol_failure() from None
            function = item.get("function", {})
            if not isinstance(function, dict):
                raise _protocol_failure() from None
            call_id = item.get("id")
            name = function.get("name")
            raw_arguments = function.get("arguments", "{}")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise _protocol_failure() from None
            arguments = _parse_tool_arguments(raw_arguments, tool_schemas.get(name))
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments={str(k): v for k, v in arguments.items()},
                )
            )
        usage_raw = raw.get("usage", {})
        if not isinstance(usage_raw, dict):
            raise _protocol_failure() from None
        usage_is_exact = (
            type(usage_raw.get("prompt_tokens")) is int
            and usage_raw["prompt_tokens"] >= 0
            and type(usage_raw.get("completion_tokens")) is int
            and usage_raw["completion_tokens"] >= 0
        )
        if usage_raw and not usage_is_exact:
            raise _protocol_failure() from None
        input_tokens = usage_raw["prompt_tokens"] if usage_is_exact else 0
        output_tokens = usage_raw["completion_tokens"] if usage_is_exact else 0
        return ModelResponse(
            content=content or "",
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason or "stop",
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
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
