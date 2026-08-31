from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from traceh.api.llm import ModelMessage, ModelRequest, ToolSchema
from traceh.api.turns import TurnInput
from traceh.llm import openai_compatible
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import AgentRuntimeExecution


def serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def answer(handler: BaseHTTPRequestHandler, content: str) -> None:
    message = {"role": "assistant", "content": content}
    body = json.dumps(
        {
            "id": "response-1",
            "choices": [{"finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.mark.asyncio
async def test_openai_compatible_provider_serializes_tools_and_parses_calls() -> None:
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            captured.update(json.loads(self.rfile.read(length).decode("utf-8")))
            payload = {
                "id": "response-1",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "inspect",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"a.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        response = await provider.complete(
            ModelRequest(
                provider=provider.name,
                model="test-model",
                system_prompt="system",
                messages=(ModelMessage("user", "read it"),),
                tools=(
                    ToolSchema(
                        "read_file",
                        "read",
                        {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    ),
                ),
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert captured["model"] == "test-model"
    assert captured["messages"][0] == {"role": "system", "content": "system"}  # type: ignore[index]
    assert captured["tools"][0]["function"]["name"] == "read_file"  # type: ignore[index]
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.usage.total_tokens == 14


@pytest.mark.asyncio
async def test_provider_normalizes_only_triple_quoted_tool_string_values() -> None:
    """Some compatible models use Python multiline delimiters in JSON arguments."""

    arguments = (
        '{"path": "module.py", "old_text": """'
        '\\"\\"\\"before\\"\\"\\"\\nvalue = 1\\n'
        '""", "new_text": """'
        '\\"\\"\\"after\\"\\"\\"\\nvalue = 2\\n'
        '"""}'
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {
                    "id": "response-multiline",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-multiline",
                                        "type": "function",
                                        "function": {
                                            "name": "apply_patch",
                                            "arguments": arguments,
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        response = await provider.complete(
            ModelRequest(
                provider=provider.name,
                model="test-model",
                messages=(ModelMessage("user", "update it"),),
                tools=(
                    ToolSchema(
                        "apply_patch",
                        "replace text",
                        {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                        },
                    ),
                ),
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert response.tool_calls[0].name == "apply_patch"
    assert response.tool_calls[0].arguments == {
        "path": "module.py",
        "old_text": '"""before"""\nvalue = 1\n',
        "new_text": '"""after"""\nvalue = 2\n',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        '{"path": "unterminated}',
        '{"unknown": """value"""}',
        '{"count": """1"""}',
        '{"payload": {"text": """value"""}}',
        "{'path': '''value'''}",
        '{"path": """value""",}',
        '{"path": """value""", "new_text": make_value()}',
        '{"path": """unterminated}',
        '{"path": "value", "count": NaN}',
        '{"path": """value""", "count": NaN}',
        '{"path": """value""", "count": Infinity}',
        '{"path": """value""", "count": -Infinity}',
    ],
    ids=(
        "truncated-json",
        "undeclared-property",
        "non-string-property",
        "nested-property",
        "single-quoted-python",
        "trailing-comma",
        "expression",
        "unterminated-triple-quote",
        "strict-json-nan",
        "normalized-json-nan",
        "normalized-json-positive-infinity",
        "normalized-json-negative-infinity",
    ),
)
async def test_other_malformed_tool_arguments_still_fail_closed(
    arguments: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-broken",
                                        "type": "function",
                                        "function": {
                                            "name": "apply_patch",
                                            "arguments": arguments,
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.complete(
                ModelRequest(
                    provider=provider.name,
                    model="test-model",
                    messages=(ModelMessage("user", "update it"),),
                    tools=(
                        ToolSchema(
                            "apply_patch",
                            "replace text",
                            {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                    "count": {"type": "integer"},
                                    "payload": {
                                        "type": "object",
                                        "properties": {
                                            "text": {"type": "string"}
                                        },
                                    },
                                },
                            },
                        ),
                    ),
                )
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert caught.value.code == "provider-tool-arguments-invalid"
    assert caught.value.category is ProviderFailureCategory.PROTOCOL


@pytest.mark.asyncio
async def test_runtime_executes_a_normalized_multiline_tool_call(tmp_path) -> None:
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal requests
            self.rfile.read(int(self.headers["Content-Length"]))
            requests += 1
            if requests == 1:
                arguments = (
                    '{"path": "module.py", "old_text": """before\\n""", '
                    '"new_text": """after\\n"""}'
                )
                body = json.dumps(
                    {
                        "id": "response-edit",
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call-edit",
                                            "type": "function",
                                            "function": {
                                                "name": "apply_patch",
                                                "arguments": arguments,
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    }
                ).encode("utf-8")
            else:
                body = json.dumps(
                    {
                        "id": "response-done",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "done"},
                            }
                        ],
                        "usage": {"prompt_tokens": 6, "completion_tokens": 1},
                    }
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("before\n", encoding="utf-8")
    server, thread = serve(Handler)
    provider = OpenAICompatibleProvider(
        f"http://127.0.0.1:{server.server_port}/v1",
        api_key="test-key",
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider=provider.name,
            model="test-model",
            max_steps=3,
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    execution = AgentRuntimeExecution(runtime, session_id)
    try:
        result = await execution.run_turn(TurnInput("update the file", "message-root"))
        events = await runtime.sessions.read_session(session_id)
    finally:
        await execution.dispose()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.reason == "completed"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert [event.type for event in events].count("tool/call") == 1
    assert [event.type for event in events].count("tool/result") == 1


@pytest.mark.asyncio
async def test_cancelling_a_request_waits_for_the_http_worker(monkeypatch) -> None:
    """A cancelled model call must not leave an HTTP worker running.

    `urllib` cannot be aborted once it is in flight, so the contract is
    convergence rather than an instant abort: the caller is released only after
    the worker has finished, and it is still released with CancelledError.
    """

    order: list[str] = []
    reached_server = threading.Event()
    release_server = threading.Event()
    converging = asyncio.Event()

    original_convergence = openai_compatible.await_worker_convergence

    async def observed_convergence(future):
        converging.set()
        return await original_convergence(future)

    monkeypatch.setattr(openai_compatible, "await_worker_convergence", observed_convergence)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            order.append("worker-in-flight")
            reached_server.set()
            release_server.wait(30)
            answer(self, "late answer nobody will read")
            order.append("worker-finished")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
            timeout_seconds=30.0,
        )
        request = ModelRequest(
            provider=provider.name,
            model="test-model",
            messages=(ModelMessage("user", "hello"),),
        )
        call = asyncio.create_task(provider.complete(request))
        assert await asyncio.to_thread(reached_server.wait, 15)

        call.cancel()
        # Deterministic proof that the convergence wait has started, rather than
        # a guessed delay.
        await asyncio.wait_for(converging.wait(), timeout=15)
        # Impatient Ctrl+C: further cancellations must not detach the worker.
        for attempt in range(2):
            call.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not call.done(), f"cancel #{attempt + 2} released the caller early"

        release_server.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        order.append("caller-returned")
    finally:
        release_server.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert order == ["worker-in-flight", "worker-finished", "caller-returned"]


@pytest.mark.asyncio
async def test_http_failure_is_typed_without_body_headers_or_secret() -> None:
    exposed = "credential-material C:\\private\\provider.txt"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = exposed.encode("utf-8")
            self.send_response(401)
            self.send_header("X-Diagnostic", exposed)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="credential-material",
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.complete(
                ModelRequest(
                    provider=provider.name,
                    model="test-model",
                    messages=(ModelMessage("user", "hello"),),
                )
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert caught.value.code == "provider-http-authentication"
    assert caught.value.category is ProviderFailureCategory.AUTHENTICATION
    assert str(caught.value) == "provider-http-authentication"
    assert exposed not in str(caught.value)


@pytest.mark.asyncio
async def test_retry_after_is_reduced_to_a_numeric_hint() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429)
            self.send_header("Retry-After", "7.5")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.complete(
                ModelRequest(
                    provider=provider.name,
                    model="test-model",
                    messages=(ModelMessage("user", "hello"),),
                )
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert caught.value.category is ProviderFailureCategory.RATE_LIMITED
    assert caught.value.retry_after_seconds == 7.5


@pytest.mark.asyncio
async def test_dns_exception_text_is_not_exposed(monkeypatch) -> None:
    def fail(*args, **kwargs):
        del args, kwargs
        raise urllib.error.URLError(
            socket.gaierror(-2, "credential-material C:\\private\\dns.txt")
        )

    monkeypatch.setattr(openai_compatible.urllib.request, "urlopen", fail)
    provider = OpenAICompatibleProvider("https://provider.invalid/v1", api_key="secret")
    with pytest.raises(ProviderFailure) as caught:
        await provider.complete(
            ModelRequest(
                provider=provider.name,
                model="test-model",
                messages=(ModelMessage("user", "hello"),),
            )
        )

    assert caught.value.category is ProviderFailureCategory.DNS
    assert str(caught.value) == "provider-dns-temporary"
    assert caught.value.usage is not None
    assert caught.value.usage.quality.value == "exact"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        json.dumps({"choices": [{"finish_reason": "stop"}]}).encode("utf-8"),
        json.dumps(
            {
                "choices": [
                    {"message": {"content": "done"}, "finish_reason": "stop"}
                ],
                "usage": None,
            }
        ).encode("utf-8"),
    ],
    ids=("invalid-json", "missing-message", "explicit-null-usage"),
)
async def test_invalid_response_is_non_retryable_protocol_failure(
    body: bytes,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.complete(
                ModelRequest(
                    provider=provider.name,
                    model="test-model",
                    messages=(ModelMessage("user", "hello"),),
                )
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert caught.value.code == "provider-response-invalid"
    assert caught.value.category is ProviderFailureCategory.PROTOCOL


@pytest.mark.asyncio
async def test_malformed_usage_is_sanitized_as_a_protocol_failure() -> None:
    exposed = "credential-material C:\\private\\usage.txt"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": exposed,
                        "completion_tokens": 1,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(Handler)
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.complete(
                ModelRequest(
                    provider=provider.name,
                    model="test-model",
                    messages=(ModelMessage("user", "hello"),),
                )
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert caught.value.category is ProviderFailureCategory.PROTOCOL
    assert exposed not in str(caught.value)


@pytest.mark.asyncio
async def test_adapter_failure_leaves_only_sanitized_durable_facts(
    monkeypatch,
    tmp_path,
) -> None:
    exposed = "credential-material C:\\private\\transport.txt"

    def fail(*args, **kwargs):
        del args, kwargs
        raise urllib.error.URLError(socket.gaierror(-2, exposed))

    monkeypatch.setattr(openai_compatible.urllib.request, "urlopen", fail)
    provider = OpenAICompatibleProvider("https://provider.invalid/v1", api_key=exposed)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider=provider.name,
            model="test-model",
            max_steps=1,
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(tmp_path)
    execution = AgentRuntimeExecution(runtime, session_id)
    try:
        with pytest.raises(ProviderFailure):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
        events = await runtime.sessions.read_session(session_id)
    finally:
        await execution.dispose()

    serialized = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    runtime_error = next(event for event in events if event.type == "runtime/error")
    assert exposed not in serialized
    assert "C:\\private" not in serialized
    assert attempt_end.data["failure_code"] == "provider-dns-temporary"
    assert "message" not in attempt_end.data
    assert "error_type" not in attempt_end.data
    assert runtime_error.data["traceback"] == "ProviderFailure: provider-dns-temporary"
