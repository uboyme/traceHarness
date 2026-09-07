"""Host History requests use the actual request and Session append owners."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from traceh.api.history import HistoryCursor, HistoryPageRequest, HistoryReadPolicy
from traceh.api.llm import ModelResponse
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.context_input import ContextInputPolicy
from traceh.session.event_store import InMemoryEventStore
from traceh.session.history import read_history
from traceh.session.history_requests import (
    HistoryRequestError,
    HistoryRequestWriteError,
    eligible_history_requests,
    event_ref,
    validate_history_request_events,
)
from traceh.session.service import SessionService


class ControlledProvider:
    name = "scripted"

    def __init__(self):
        self.requests = []
        self.response = ModelResponse(content="recorded answer")

    async def complete(self, request):
        self.requests.append(request)
        response = self.response
        self.response = ModelResponse(content="continued answer")
        return response


def explicit_policy(**changes):
    return HistoryReadPolicy(
        **(
            {
                "max_blocks": 12,
                "max_depth": 8,
                "page_bytes": 16_000,
                "page_messages": 8,
                "max_source_events": 400,
                "max_source_bytes": 2_000_000,
                "max_requests": 3,
            }
            | changes
        )
    )


async def disclosed(
    tmp_path,
    *,
    store=None,
    subject="old telemetry",
    tier="directory",
    policy_changes=None,
    history_turns=1,
):
    policy = explicit_policy(**(policy_changes or {}))
    context = ContextInputPolicy(
        history_tier=tier,
        total_bytes=64_000,
        history_bytes=60_000,
        item_bytes=50_000,
        max_blocks=12,
        max_exclusions=20,
        max_query_bytes=4_000,
        history=policy,
    )
    provider = ControlledProvider()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=context),
        event_store=store if store is not None else InMemoryEventStore(),
        provider=provider,
    )
    first = await runtime.run(tmp_path, subject)
    for number in range(1, history_turns):
        await runtime.run_existing(first.session_id, f"another recorded input {number}")
    events = await runtime.sessions.read_session(first.session_id)
    await runtime.compaction.replace_through(
        first.session_id,
        through_seq=events[-1].seq,
        summary="bounded past summary",
    )
    await runtime.run_existing(first.session_id, "show the catalog")
    events = await runtime.sessions.read_session(first.session_id)
    block = read_history(
        events,
        session_id=first.session_id,
        through_seq=events[-1].seq,
        policy=policy,
    ).directory()[0]
    request = HistoryPageRequest(block.block_id, block.first_cursor, "section")
    return runtime, provider, first.session_id, policy, request


async def open_host_step(sessions, session_id, *, turn_id="host-turn", step_id="host-step"):
    await sessions.append_session(session_id, "turn/start", {"turn_id": turn_id})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": turn_id, "step_id": step_id, "number": 1},
    )
    user = await sessions.append_session(
        session_id,
        "user/message",
        {"turn_id": turn_id, "step_id": step_id, "content": "explicit page selection"},
    )
    return {"turn_id": turn_id, "step_id": step_id, "user_message_ref": event_ref(user)}


@pytest.mark.parametrize(
    "tier,subject", [("directory", "old telemetry"), ("summary", "接口旧证据")]
)
async def test_host_request_is_atomic_deduplicated_and_first_step_only(tmp_path, tier, subject):
    runtime, _, sid, policy, request = await disclosed(tmp_path, tier=tier, subject=subject)
    try:
        scope = await open_host_step(runtime.sessions, sid)
        recorded = await runtime.sessions.append_history_requests(
            sid,
            **scope,
            requests=(request, replace(request, requested_tier="chunk")),
            policy=policy,
        )
        assert len(recorded) == 1
        events = await runtime.sessions.read_session(sid)
        validate_history_request_events(events)
        eligible = eligible_history_requests(
            events,
            session_id=sid,
            turn_id=scope["turn_id"],
            step_id=scope["step_id"],
            policy=policy,
        )
        assert eligible[0].request == request
        assert eligible[0].request_ref == event_ref(recorded[0])
        await runtime.sessions.append_session(
            sid,
            "step/end",
            {"turn_id": scope["turn_id"], "step_id": scope["step_id"], "reason": "model_response"},
        )
        await runtime.sessions.append_session(
            sid,
            "step/start",
            {"turn_id": scope["turn_id"], "step_id": "later-step", "number": 2},
        )
        assert (
            eligible_history_requests(
                await runtime.sessions.read_session(sid),
                session_id=sid,
                turn_id=scope["turn_id"],
                step_id="later-step",
                policy=policy,
            )
            == ()
        )
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("change", ["cursor", "user-ref", "request-limit"])
async def test_host_request_rejects_undisclosed_or_wrong_binding_without_write(tmp_path, change):
    runtime, _, sid, policy, request = await disclosed(tmp_path)
    try:
        scope = await open_host_step(runtime.sessions, sid)
        requests = (request,)
        if change == "cursor":
            request = replace(request, cursor=HistoryCursor(request.block_id, policy.digest, 1))
            requests = (request,)
        elif change == "user-ref":
            scope["user_message_ref"] = {**scope["user_message_ref"], "event_id": "unrelated"}
        else:
            requests = (request,) * (policy.max_requests + 1)
        before = await runtime.sessions.read_session(sid)
        with pytest.raises(HistoryRequestError):
            await runtime.sessions.append_history_requests(
                sid, **scope, requests=requests, policy=policy
            )
        assert await runtime.sessions.read_session(sid) == before
    finally:
        await runtime.dispose()


async def test_host_can_select_exact_disclosed_block_hidden_by_later_compaction(tmp_path):
    runtime, _, sid, policy, request = await disclosed(tmp_path)
    try:
        events = await runtime.sessions.read_session(sid)
        await runtime.compaction.replace_through(
            sid, through_seq=events[-1].seq, summary="wider summary"
        )
        scope = await open_host_step(runtime.sessions, sid)
        recorded = await runtime.sessions.append_history_requests(
            sid,
            **scope,
            requests=(request,),
            policy=policy,
        )
        assert recorded[0].data["block_id"] == request.block_id
        validate_history_request_events(await runtime.sessions.read_session(sid))
    finally:
        await runtime.dispose()


class HistoryFaultStore(InMemoryEventStore):
    def __init__(self, *, committed, unknown=False, gated=False):
        super().__init__()
        self.committed, self.unknown, self.gated = committed, unknown, gated
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.converged = False
        self.read_before_convergence = False

    async def append(self, stream_id, *, expected_seq, events, durability):
        if not any(event.type == "history/requested" for event in events):
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )
        self.entered.set()
        if self.gated:
            await self.release.wait()
        if self.committed:
            await super().append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )
        self.converged = True
        raise OSError("fixture append acknowledgment failed")

    async def read(self, stream_id, *, from_seq=1):
        if self.entered.is_set() and not self.converged:
            self.read_before_convergence = True
        if self.converged and self.unknown:
            raise OSError("fixture reconciliation unavailable")
        return await super().read(stream_id, from_seq=from_seq)


@pytest.mark.parametrize(
    "commit,unknown,expected", [(False, False, False), (True, False, True), (True, True, None)]
)
async def test_history_batch_preserves_three_commit_outcomes(tmp_path, commit, unknown, expected):
    store = HistoryFaultStore(committed=commit, unknown=unknown)
    runtime, _, sid, policy, request = await disclosed(tmp_path, store=store)
    try:
        scope = await open_host_step(runtime.sessions, sid)
        with pytest.raises(HistoryRequestWriteError) as caught:
            await runtime.sessions.append_history_requests(
                sid, **scope, requests=(request,), policy=policy
            )
        assert store.entered.is_set()
        assert caught.value.committed is expected
        store.unknown = False
        assert (
            sum(e.type == "history/requested" for e in await runtime.sessions.read_session(sid))
            == commit
        )
    finally:
        store.unknown = False
        await runtime.dispose()


@pytest.mark.parametrize("commit", [False, True])
async def test_repeated_cancel_converges_history_batch_before_reconciliation(tmp_path, commit):
    store = HistoryFaultStore(committed=commit, gated=True)
    runtime, _, sid, policy, request = await disclosed(tmp_path, store=store)
    try:
        scope = await open_host_step(runtime.sessions, sid)
        task = asyncio.create_task(
            runtime.sessions.append_history_requests(
                sid,
                **scope,
                requests=(request,),
                policy=policy,
            )
        )
        await asyncio.wait_for(store.entered.wait(), 5)
        task.cancel()
        cycle = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycle.set)
        await cycle.wait()
        task.cancel()
        assert not task.done()
        store.release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert isinstance(caught.value.__cause__, HistoryRequestWriteError)
        assert caught.value.__cause__.committed is commit
        assert not store.read_before_convergence
    finally:
        store.release.set()
        await runtime.dispose()


async def test_competing_history_writers_cannot_both_accept_same_user_input(tmp_path):
    runtime, _, sid, policy, request = await disclosed(tmp_path)
    try:
        scope = await open_host_step(runtime.sessions, sid)
        competitor = SessionService(runtime.sessions.store)
        results = await asyncio.gather(
            *(
                owner.append_history_requests(sid, **scope, requests=(request,), policy=policy)
                for owner in (runtime.sessions, competitor)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert sum(isinstance(result, Exception) for result in results) == 1
        assert (
            sum(e.type == "history/requested" for e in await runtime.sessions.read_session(sid))
            == 1
        )
    finally:
        await runtime.dispose()
