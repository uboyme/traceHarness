import json
from dataclasses import replace

import pytest
from test_product_f3_e2e import _response

from traceh.api.llm import ToolCall
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import build_request_from_events, verify_request_snapshots
from traceh.runtime.step_view import StepViewSelection
from traceh.session.event_store import InMemoryEventStore
from traceh.session.request_view import RequestView
from traceh.session.surface import SurfaceProjector


class ViewPolicy:
    async def select(self, events, composition, **kwargs):
        del kwargs
        if not any(e.type == "tool/result" for e in events):
            return StepViewSelection(
                RequestView("inspect", ("read_file",), "Read once.", max_calls=1)
            )
        return StepViewSelection(RequestView("decide", (), "Analyze only.", "evidence", 1))


class AlwaysReadOnly:
    async def select(self, events, composition, **kwargs):
        del events, composition, kwargs
        return StepViewSelection(RequestView("inspect", ("read_file",), "Read only.", max_calls=2))


class ViewProvider:
    name = "view-test"

    def __init__(self, mixed=False):
        self.requests = []
        self.mixed = mixed

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            calls = [ToolCall("read", "read_file", {"path": "source.txt"})]
            if self.mixed:
                calls.append(
                    ToolCall(
                        "write",
                        "apply_patch",
                        {"path": "escaped.txt", "old_text": "", "new_text": "bad", "create": True},
                    )
                )
            return _response("old execution narrative", *calls)
        assert request.tools == ()
        assert len(request.messages) == 1
        assert "old execution narrative" not in request.messages[0].content
        packet = json.loads(request.messages[0].content.split("\n", 1)[1])
        assert len(packet["observations"]) == (2 if self.mixed else 1)
        if not self.mixed:
            assert "visible fact" in packet["observations"][0]["body"]
        return _response("finished")


class InventedToolProvider:
    """Calls a name no tool has, then a real tool this Step does not expose."""

    name = "view-test"

    def __init__(self):
        self.requests = []
        self.denials = []

    async def complete(self, request):
        self.requests.append(request)
        for message in request.messages:
            if message.name in {"write_file", "apply_patch"}:
                self.denials.append(message.content)
        if len(self.requests) == 1:
            return _response("", ToolCall("invent", "write_file", {"path": "a", "text": "b"}))
        if len(self.requests) == 2:
            return _response(
                "",
                ToolCall(
                    "hidden",
                    "apply_patch",
                    {"path": "a", "old_text": "", "new_text": "b", "create": True},
                ),
            )
        return _response("finished")


@pytest.mark.asyncio
async def test_a_refused_batch_names_the_mistake_and_what_is_callable(tmp_path):
    """A caller can only repair a refusal it can tell apart.

    An invented name and a real tool this Step hides are different mistakes.
    Reporting both as one view problem leaves nothing to act on, which is how
    an invented tool name gets retried instead of dropped.
    """

    (tmp_path / "source.txt").write_text("visible fact", encoding="utf-8")
    provider = InventedToolProvider()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider=provider.name, model="model"),
        provider=provider,
        event_store=InMemoryEventStore(),
        step_view=AlwaysReadOnly(),
    )
    try:
        sid = await runtime.create_session(tmp_path)
        await runtime.run_existing(sid, "Inspect the source")
        events = await runtime.sessions.read_session(sid)
    finally:
        await runtime.dispose()

    results = [e for e in events if e.type == "tool/result"]
    assert [e.data["status"] for e in results] == ["denied", "denied"]
    invented, hidden = (e.data["content"] for e in results)

    # The invented name is reported as a name no tool has.
    assert "No tool is named write_file." in invented
    assert "Not callable in this Step" not in invented
    # The real-but-hidden tool is reported as unavailable here, not as absent.
    assert "Not callable in this Step: apply_patch." in hidden
    assert "No tool is named" not in hidden
    # Both say what this Step can actually call.
    for content in (invented, hidden):
        assert "Callable here: read_file." in content
    assert not (tmp_path / "a").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_request_view_freezes_input_enforces_batch_and_replays(tmp_path, mixed):
    (tmp_path / "source.txt").write_text("visible fact", encoding="utf-8")
    provider = ViewProvider(mixed)
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider=provider.name, model="model"),
        provider=provider,
        event_store=InMemoryEventStore(),
        step_view=ViewPolicy(),
    )
    try:
        sid = await runtime.create_session(tmp_path)
        await runtime.run_existing(sid, "Inspect the source")
        events = await runtime.sessions.read_session(sid)
        results = [e for e in events if e.type == "tool/result"]
        assert results and all(
            e.data["status"] == ("denied" if mixed else "succeeded") for e in results
        )
        assert not (tmp_path / "escaped.txt").exists()
        assert len(provider.requests) == 2
        assert not await verify_request_snapshots(runtime.sessions, SurfaceProjector(), sid)
        assert not await runtime.check_invariants(sid)
        snapshot = [e for e in events if e.type == "request/snapshot"][-1]
        views = [e for e in events if e.type == "request/view"]
        target = views[-1]
        damaged = tuple(
            replace(e, data={**e.data, "source_digest": "0" * 64}) if e.seq == target.seq else e
            for e in events
        )
        with pytest.raises(ValueError, match="request-view-source-mismatch"):
            build_request_from_events(
                damaged,
                SurfaceProjector(),
                session_id=sid,
                turn_id=snapshot.data["turn_id"],
                step_id=snapshot.data["step_id"],
                through_seq=snapshot.data["source_seq"],
            )
    finally:
        await runtime.dispose()
