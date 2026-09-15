"""Real adapter/Runtime failure evidence over local HTTP; no external service."""

import json
from http.server import BaseHTTPRequestHandler

import pytest
from test_openai_provider import serve

from traceh.llm.failures import ProviderFailure
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.surface import SurfaceProjector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,reason",
    [
        (None, "type-invalid"),
        ("[]", "not-object"),
        ('"private-fixture-value"', "not-object"),
        ('{"path":"private-fixture-value" "x":1}', "json-comma-expected"),
        ('{"path" "private-fixture-value"}', "json-colon-expected"),
        ("{'path':'private-fixture-value'}", "json-key-expected"),
        ('{"path":"private-fixture-value', "json-string-unclosed"),
        ('{"path":}', "json-value-expected"),
        ("{} private-fixture-value", "json-extra-data"),
        ('{"path":"private-fixture-value\n"}', "json-control-character"),
        ('{"path":"\\qprivate-fixture-value"}', "json-escape-invalid"),
        ('{"path":"\\uXXXXprivate-fixture-value"}', "json-unicode-escape-invalid"),
        ('{"path":NaN}', "json-nonfinite"),
    ],
)
async def test_diagnosis_is_durable_sanitized_and_never_executes(tmp_path, arguments, reason):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "private-fixture-value",
                                "tool_calls": [
                                    {
                                        "id": "private-fixture-value",
                                        "function": {"name": "read_file", "arguments": arguments},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            del args

    server, thread = serve(Handler)
    provider = OpenAICompatibleProvider(f"http://127.0.0.1:{server.server_port}/v1")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider=provider.name, model="fixture"),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    try:
        sid = await runtime.create_session(tmp_path)
        with pytest.raises(ProviderFailure) as caught:
            await runtime.run_existing(sid, "Read a note")
        expected = "provider-tool-arguments-" + reason
        assert str(caught.value) == expected
        events = await runtime.sessions.read_session(sid)
        failed = [e for e in events if e.type == "model/attempt-end"]
        assert len(failed) == len(calls) == 1
        assert failed[0].data["failure_code"] == expected
        assert failed[0].data["failure_category"] == "protocol"
        assert not any(e.type in {"tool/call", "assistant/message"} for e in events)
        assert "private-fixture-value" not in json.dumps([e.data for e in events])
        assert not await verify_request_snapshots(runtime.sessions, SurfaceProjector(), sid)
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert not thread.is_alive()
