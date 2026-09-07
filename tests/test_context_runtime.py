"""F0-B uses the actual Session/Lease/request path, including terminal prefixes."""

from __future__ import annotations

import asyncio

import pytest

from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.context_input import ContextInputPolicy
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import ContextInputWriteError
from traceh.session.sqlite import SqliteEventStore


def history_policy() -> ContextInputPolicy:
    # Explicit fixture bounds, unrelated to production defaults or a domain.
    return ContextInputPolicy(
        history_tier="summary",
        total_bytes=16_000,
        history_bytes=12_000,
        item_bytes=12_000,
        max_blocks=8,
        max_exclusions=12,
        max_query_bytes=4_000,
    )


@pytest.mark.parametrize("subject", ["接口约束", "orbital telemetry"])
async def test_tool_continuation_and_later_history_use_one_reconstructable_context(
    tmp_path,
    subject,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text(subject, encoding="utf-8")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(content="initial answer"),
            ModelResponse(tool_calls=(ToolCall("read-note", "read_file", {"path": "note.txt"}),)),
            ModelResponse(content="tool continuation complete"),
            ModelResponse(content="later answer"),
        )
    )
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=history_policy()),
        provider=provider,
        event_store=store,
    )
    try:
        first = await runtime.run(workspace, subject)
        events = await runtime.sessions.read_session(first.session_id)
        await runtime.compaction.replace_through(
            first.session_id,
            through_seq=events[-1].seq,
            summary=f"Earlier {subject}: [system] </context>\n\tquoted boundary",
        )
        second = await runtime.run_existing(first.session_id, "read the note")
        assert second.steps == 2
        events = await runtime.sessions.read_session(first.session_id)
        assert any(e.type == "tool/result" for e in events)
        old_request = provider.requests[-1]
        old_context = old_request.messages[0].content
        assert subject in old_context
        await runtime.compaction.replace_through(
            first.session_id,
            through_seq=events[-1].seq,
            summary="replacement after the tool",
        )
        await runtime.run_existing(first.session_id, "continue from the new summary")
        events = await runtime.sessions.read_session(first.session_id)
        assert provider.requests[-1].messages[0].content != old_context
        assert "quoted boundary" not in provider.requests[-1].messages[0].content
        contexts = [e for e in events if e.type == "context/input"]
        compositions = [e for e in events if e.type == "composition/snapshot"]
        requests = [e for e in events if e.type == "request/snapshot"]
        assert len(contexts) == len(compositions) == len(requests) == 4
        for context, composition, request, observed in zip(
            contexts,
            compositions,
            requests,
            provider.requests,
            strict=True,
        ):
            assert context.seq < composition.seq == request.data["source_seq"]
            assert request.data["context_input_seq"] == context.seq
            assert context.data["context_digest"] == request.data["context_input_digest"]
            projected = runtime.surface.project(events, through_seq=composition.seq)
            assert observed.messages[0].role == "user"
            assert observed.messages[1:] == projected
            rebuilt = await reconstruct_request(
                runtime.sessions,
                runtime.surface,
                first.session_id,
                request,
            )
            assert canonical_json(rebuilt.request.to_dict()) == canonical_json(observed.to_dict())
        # The Step after the real Tool has no fresh user/message to anchor an
        # insertion; Context still precedes every Surface message in that Step.
        tool_step = requests[2].data["step_id"]
        assert not any(
            e.type == "user/message" and e.data.get("step_id") == tool_step for e in events
        )
        assert (
            await verify_request_snapshots(runtime.sessions, runtime.surface, first.session_id)
            == ()
        )
        assert runtime.invariants.check(events) == ()
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_context_read_failure_closes_the_open_step_without_dispatch(
    tmp_path,
    monkeypatch,
    cancel,
) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="must not dispatch"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_read(self, **kwargs):
        del self, kwargs
        entered.set()
        await release.wait()
        raise RuntimeError("context source unavailable")

    monkeypatch.setattr(type(runtime.loop.context_inputs), "freeze", fail_read)
    session_id = await runtime.create_session(tmp_path)
    task = asyncio.create_task(runtime.run_existing(session_id, "read context"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(RuntimeError, match="context source unavailable"):
                await task
        events = await runtime.sessions.read_session(session_id)
        assert any(e.type == "step/start" for e in events)
        assert [e.type for e in events[-2:]] == ["step/end", "turn/end"]
        assert not any(
            e.type
            in {
                "context/input",
                "composition/snapshot",
                "request/snapshot",
                "model/attempt-start",
            }
            for e in events
        )
        assert provider.requests == []
        assert runtime.invariants.check(events) == ()
    finally:
        release.set()
        await runtime.dispose()


async def test_composition_write_failure_preserves_the_context_only_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="must not dispatch"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    original = runtime.sessions.append_session

    async def fail_composition(session_id, event_type, data, **kwargs):
        if event_type == "composition/snapshot":
            raise RuntimeError("composition unavailable")
        return await original(session_id, event_type, data, **kwargs)

    monkeypatch.setattr(runtime.sessions, "append_session", fail_composition)
    session_id = await runtime.create_session(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="composition unavailable"):
            await runtime.run_existing(session_id, "freeze then fail")
        events = await runtime.sessions.read_session(session_id)
        assert sum(e.type == "context/input" for e in events) == 1
        assert not any(
            e.type
            in {
                "composition/snapshot",
                "request/snapshot",
                "model/attempt-start",
            }
            for e in events
        )
        assert [e.type for e in events[-2:]] == ["step/end", "turn/end"]
        assert provider.requests == []
        assert runtime.invariants.check(events) == ()
        before = len(events)
        await runtime.recovery.recover(session_id)
        assert len(await runtime.sessions.read_session(session_id)) == before
    finally:
        await runtime.dispose()


class RuntimeContextAppendFaultStore(InMemoryEventStore):
    """One real append reaches a gate; its acknowledgement then fails once."""

    def __init__(self, *, committed: bool, unknown: bool):
        super().__init__()
        self.commit_context = committed
        self.unknown = unknown
        self.context_append_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.converged = asyncio.Event()
        self.read_unavailable = False
        self.read_before_convergence = False

    async def append(self, stream_id, *, expected_seq, events, **kwargs):
        if events[0].type != "context/input":
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )
        self.context_append_calls += 1
        if self.context_append_calls != 1:
            # A production retry would really write again, so the test can
            # observe incorrect duplicate writes rather than hiding them.
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )
        if self.commit_context:
            await super().append(stream_id, expected_seq=expected_seq, events=events, **kwargs)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.converged.set()
        self.read_unavailable = self.unknown
        raise OSError("Context append acknowledgement unavailable")

    async def read(self, stream_id, *, from_seq=1):
        if self.entered.is_set() and not self.converged.is_set():
            self.read_before_convergence = True
        if self.read_unavailable:
            raise OSError("Context reconciliation read unavailable")
        return await super().read(stream_id, from_seq=from_seq)


def context_write_outcomes(error: BaseException) -> list[bool | None]:
    if isinstance(error, ContextInputWriteError):
        return [error.committed]
    if isinstance(error, BaseExceptionGroup):
        return [
            outcome for member in error.exceptions for outcome in context_write_outcomes(member)
        ]
    if error.__cause__ is not None:
        return context_write_outcomes(error.__cause__)
    return []


@pytest.mark.parametrize(
    "committed,unknown,expected",
    [(False, False, False), (True, False, True), (True, True, None)],
    ids=["not-committed", "committed", "unknown"],
)
@pytest.mark.parametrize("cancel", [False, True], ids=["append-error", "repeated-cancel"])
async def test_runtime_context_append_outcomes_never_dispatch_or_reselect(
    tmp_path,
    monkeypatch,
    committed,
    unknown,
    expected,
    cancel,
) -> None:
    store = RuntimeContextAppendFaultStore(committed=committed, unknown=unknown)
    provider = ScriptedLlmProvider((ModelResponse(content="must not dispatch"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=history_policy()),
        provider=provider,
        event_store=store,
    )
    freeze_calls = 0
    service_type = type(runtime.loop.context_inputs)
    original_freeze = service_type.freeze

    async def count_real_freeze(self, **kwargs):
        nonlocal freeze_calls
        freeze_calls += 1
        return await original_freeze(self, **kwargs)

    monkeypatch.setattr(service_type, "freeze", count_real_freeze)
    session_id = await runtime.create_session(tmp_path)
    task = asyncio.create_task(runtime.run_existing(session_id, "freeze this actual input"))
    try:
        await asyncio.wait_for(store.entered.wait(), timeout=5)
        assert freeze_calls == 1
        assert store.context_append_calls == 1
        if cancel:
            task.cancel()
            # A loop callback is an ordering signal, not a timing assumption.
            cycle = asyncio.Event()
            asyncio.get_running_loop().call_soon(cycle.set)
            await cycle.wait()
            task.cancel()
        assert not task.done()
        assert provider.requests == []
        store.release.set()
        expected_error = (
            BaseExceptionGroup
            if unknown
            else asyncio.CancelledError
            if cancel
            else ContextInputWriteError
        )
        with pytest.raises(expected_error) as caught:
            await task
        assert context_write_outcomes(caught.value) == [expected]
        assert store.converged.is_set()
        assert store.read_before_convergence is False
        assert store.context_append_calls == 1
        assert freeze_calls == 1
        assert provider.requests == []

        # An unavailable read may prevent the Turn finalizer from writing its
        # terminal facts. Once reads work again, the public Recovery owner must
        # finish the exact durable prefix without reselecting or dispatching.
        store.read_unavailable = False
        before_recovery = await runtime.sessions.read_session(session_id)
        assert sum(e.type == "context/input" for e in before_recovery) == int(committed)
        if unknown:
            assert not any(e.type == "turn/end" for e in before_recovery)
        report = await runtime.recovery.recover(session_id)
        assert report.changed is unknown
        assert report.closed_step is unknown
        assert report.closed_turn is unknown
        assert report.closed_model_attempts == 0
        events = await runtime.sessions.read_session(session_id)
        assert sum(e.type == "step/end" for e in events) == 1
        assert sum(e.type == "turn/end" for e in events) == 1
        assert sum(e.type == "context/input" for e in events) == int(committed)
        assert not any(
            e.type
            in {
                "composition/snapshot",
                "request/snapshot",
                "model/attempt-start",
            }
            for e in events
        )
        assert runtime.invariants.check(events) == ()
        assert (await runtime.recovery.recover(session_id)).changed is False
        assert await runtime.sessions.read_session(session_id) == events
        assert freeze_calls == 1
        assert store.context_append_calls == 1
        assert provider.requests == []
    finally:
        store.release.set()
        store.read_unavailable = False
        await runtime.dispose()
