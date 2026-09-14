"""C4 observes the public Provider's actual HTTP boundary without repairing arguments."""

import hashlib
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest
from provider_argument_probe.probe import analyze_body, record_http
from test_openai_provider import serve

from traceh.api.llm import ModelMessage, ModelRequest, ToolSchema
from traceh.llm.failures import ProviderFailure
from traceh.llm.openai_compatible import OpenAICompatibleProvider


def request():
    return ModelRequest(
        provider="openai-compatible",
        model="explicit-boundary-fixture",
        messages=(ModelMessage("user", "Read the requested record."),),
        tools=(
            ToolSchema(
                "lookup",
                "Read",
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
        ),
    )


@pytest.mark.parametrize(
    ("arguments", "syntax", "schema_status"),
    [
        ('{"value":"ok"}', "object", "valid"),
        ('{"value":17}', "object", "invalid"),
        ('{"value":', "invalid-json", None),
    ],
)
async def test_recorded_wire_distinguishes_json_parsing_and_tool_schema(
    arguments, syntax, schema_status
):
    body = json.dumps(
        {
            "id": "response-fixture",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-fixture",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": arguments},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
    ).encode()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            del args

    server, thread = serve(Handler)
    parsed = None
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}/v1", api_key="explicit-test-only"
        )
        with record_http() as captured:
            if syntax == "invalid-json":
                with pytest.raises(ProviderFailure) as error:
                    await provider.complete(request())
                assert error.value.code == "provider-tool-arguments-json-value-expected"
            else:
                parsed = await provider.complete(request())
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert not thread.is_alive() and len(received) == 1
    assert captured["body"] == body
    assert captured["request_payload_sha256"] == hashlib.sha256(received[0]).hexdigest()
    analysis = analyze_body(body, request(), parsed)
    assert analysis["calls"][0]["syntax"] == syntax
    assert analysis["calls"][0].get("tool_schema") == schema_status
    assert analysis["wire_usage"] == {"prompt_tokens": 5, "completion_tokens": 3}
    if parsed:
        assert analysis["calls"][0]["adapter_arguments_match"]
    assert not any("explicit-test-only" in str(value) for value in captured.values())


async def test_recorder_preserves_transport_failure_and_does_not_invent_body(monkeypatch):
    def unavailable(*args, **kwargs):
        del args, kwargs
        raise urllib.error.URLError(ConnectionResetError())

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    provider = OpenAICompatibleProvider("http://example.invalid/v1", api_key="explicit-test-only")
    with record_http() as captured:
        with pytest.raises(ProviderFailure):
            await provider.complete(request())
    assert captured["body"] is None and captured["request_payload_sha256"] is not None
    assert analyze_body(None, request(), None) == {
        "body_available": False,
        "calls": [],
        "wire_usage": None,
    }
