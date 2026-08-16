from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from traceh.api.llm import ModelMessage, ModelRequest, ToolSchema
from traceh.llm.openai_compatible import OpenAICompatibleProvider


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
