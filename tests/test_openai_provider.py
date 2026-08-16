from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from traceh.api.llm import ModelMessage, ModelRequest, ToolSchema
from traceh.llm import openai_compatible
from traceh.llm.openai_compatible import OpenAICompatibleProvider


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
