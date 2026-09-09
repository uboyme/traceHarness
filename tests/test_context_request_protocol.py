"""Public Session/Request boundaries for the first Context protocol."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from traceh.api.events import PendingEvent
from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelAttemptIdentity
from traceh.inspector.session_inspector import SessionInspector
from traceh.kernel.composition import RuntimeComposition
from traceh.runtime.request_builder import (
    RequestBuilder,
    composition_from_event,
    reconstruct_request,
)
from traceh.session.context_input import ContextInputService
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.protocol import CONTEXT_PROTOCOL, SessionProtocolError
from traceh.session.recovery import RecoveryService
from traceh.session.service import (
    ContextInputWriteError,
    ModelAttemptConflictError,
    SessionService,
)
from traceh.session.surface import SurfaceProjector

pytestmark = pytest.mark.asyncio


async def opened(tmp_path, *, store=None, system_prompt="Host instruction"):
    store = store if store is not None else InMemoryEventStore()
    sessions = SessionService(store)
    session_id = await sessions.create_session(tmp_path, session_id="request-boundary")
    await sessions.append_session(session_id, "turn/start", {"turn_id": "turn-one"})
    await sessions.append_session(
        session_id, "user/message", {"turn_id": "turn-one", "content": "Inspect the source."}
    )
    await sessions.append_session(
        session_id, "step/start", {"turn_id": "turn-one", "step_id": "step-one"}
    )
    composition = RuntimeComposition(
        provider="scripted", model="contract-model", system_prompt=system_prompt, tools=()
    ).snapshot()
    snapshot = await ContextInputService(sessions.read_session).freeze(
        session_id=session_id, turn_id="turn-one", step_id="step-one", composition=composition
    )
    return sessions, session_id, composition, snapshot


async def frozen(tmp_path, *, system_prompt="Host instruction"):
    sessions, session_id, composition, snapshot = await opened(
        tmp_path, system_prompt=system_prompt
    )
    context_event = await sessions.append_context_input(
        session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
    )
    composition_event = await sessions.append_session(
        session_id,
        "composition/snapshot",
        composition.to_dict(),
        composition_revision=composition.revision,
    )
    built = await RequestBuilder(sessions, SurfaceProjector()).build(
        session_id=session_id,
        turn_id="turn-one",
        step_id="step-one",
        composition=composition,
        through_seq=composition_event.seq,
    )
    return sessions, session_id, composition, context_event, composition_event, built


async def dispatch_permit(sessions, session_id, composition, built):
    return await sessions.start_model_attempt(
        session_id,
        attempt=ModelAttemptIdentity(session_id, "turn-one", "step-one", "attempt-one", 1),
        source_seq=built.source_seq,
        composition_revision=composition.revision,
        composed_request=built.request,
        composed_fingerprint=built.fingerprint,
        dispatch_request=built.request,
        dispatch_fingerprint=built.fingerprint,
        reservation_id=None,
    )


@pytest.mark.parametrize(
    "marker", [None, True, *range(CONTEXT_PROTOCOL), CONTEXT_PROTOCOL + 1, "2"]
)
async def test_old_session_is_refused_by_read_recovery_inspection_without_writes(tmp_path, marker):
    store = InMemoryEventStore()
    data = {"session_id": "old-session", "workspace": str(tmp_path), "metadata": {}}
    if marker is not None:
        data["context_protocol"] = marker
    await store.append(
        "session:old-session",
        expected_seq=0,
        events=(PendingEvent(type="session/created", data=data),),
    )
    sessions = SessionService(store)
    original = await store.read("session:old-session")
    assert await sessions.list_sessions() == ("old-session",)
    for operation in (
        lambda: sessions.read_session("old-session"),
        lambda: sessions.ensure_session("old-session"),
        lambda: sessions.workspace_for("old-session"),
        lambda: RecoveryService(sessions).recover("old-session"),
        lambda: SessionInspector(sessions, SurfaceProjector()).summary("old-session"),
        lambda: SessionInspector(sessions, SurfaceProjector()).replay_text("old-session"),
        lambda: sessions.append_session("old-session", "turn/start", {"turn_id": "new"}),
    ):
        with pytest.raises(SessionProtocolError):
            await operation()
    assert CoreInvariantChecker().check(original)[0].name == SessionProtocolError.code
    assert await store.read("session:old-session") == original


@pytest.mark.parametrize("system_prompt", ["", "Host instruction"])
async def test_one_context_is_bound_to_request_permit_and_reconstructed(tmp_path, system_prompt):
    sessions, session_id, composition, context, _, built = await frozen(
        tmp_path, system_prompt=system_prompt
    )
    snapshot, attempt = await dispatch_permit(sessions, session_id, composition, built)
    assert attempt.seq == snapshot.seq + 1
    assert snapshot.data["context_input_seq"] == context.seq
    assert snapshot.data["context_input_digest"] == context.data["context_digest"]
    rebuilt = await reconstruct_request(sessions, SurfaceProjector(), session_id, snapshot)
    assert rebuilt == built
    assert CoreInvariantChecker().check(await sessions.read_session(session_id)) == ()


async def test_request_replay_cannot_borrow_equal_revision_from_another_step(tmp_path):
    sessions, session_id, composition, _, source, built = await frozen(tmp_path)
    snapshot, _ = await dispatch_permit(sessions, session_id, composition, built)
    # A second Step may legitimately use byte-identical composition. Its source
    # boundary still cannot be the first Step's Composition event.
    forged = replace(snapshot, data={**snapshot.data, "step_id": "step-other"})
    with pytest.raises(ValueError, match="request-source-boundary-invalid"):
        await reconstruct_request(sessions, SurfaceProjector(), session_id, forged)
    wrong_boundary = replace(snapshot, data={**snapshot.data, "source_seq": source.seq - 1})
    with pytest.raises(ValueError, match="request-source-boundary-invalid"):
        await reconstruct_request(sessions, SurfaceProjector(), session_id, wrong_boundary)


@pytest.mark.parametrize("change", ["old-format", "catalog", "catalog-digest", "revision"])
async def test_composition_reader_rejects_invalid_catalog_and_content_drift(tmp_path, change):
    _, _, _, _, source, _ = await frozen(tmp_path)
    data = dict(source.data)
    if change == "old-format":
        del data["skill_catalog"]
        del data["skill_catalog_digest"]
    elif change == "catalog":
        data["skill_catalog"] = [{"id": "explicit-fixture-skill"}]
        data["skill_catalog_digest"] = fingerprint(data["skill_catalog"])
    elif change == "catalog-digest":
        data["skill_catalog_digest"] = "f" * 64
    else:
        data["system_prompt"] = "Changed recorded instructions"
    if change != "revision":
        # An internally consistent hash cannot authorize an invalid catalog or
        # silently turn the old Composition format into the current protocol.
        data["revision"] = fingerprint({k: v for k, v in data.items() if k != "revision"})
    forged = replace(source, data=data, composition_revision=data["revision"])
    with pytest.raises(ValueError, match="composition-|skill-descriptor-"):
        composition_from_event(forged)


@pytest.mark.parametrize("field", ["context_input_seq", "context_input_digest"])
async def test_forged_context_metadata_cannot_obtain_dispatch_permit(tmp_path, field):
    sessions, session_id, composition, _, _, built = await frozen(tmp_path)
    metadata = dict(built.request.metadata)
    metadata[field] = 1 if field.endswith("seq") else "f" * 64
    forged = replace(built.request, metadata=metadata)
    forged_built = replace(built, request=forged, fingerprint=fingerprint(forged.to_dict()))
    head = await sessions.store.head(sessions.session_stream(session_id))
    with pytest.raises(ModelAttemptConflictError):
        await dispatch_permit(sessions, session_id, composition, forged_built)
    assert await sessions.store.head(sessions.session_stream(session_id)) == head


@pytest.mark.parametrize("prefix", ["step", "context", "composition"])
async def test_requestless_failure_prefix_remains_recoverable(tmp_path, prefix):
    sessions, session_id, composition, snapshot = await opened(tmp_path)
    if prefix != "step":
        await sessions.append_context_input(
            session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
        )
    if prefix == "composition":
        await sessions.append_session(
            session_id,
            "composition/snapshot",
            composition.to_dict(),
            composition_revision=composition.revision,
        )
    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    assert CoreInvariantChecker().check(events) == ()
    assert not any(event.type in {"request/snapshot", "model/attempt-start"} for event in events)
    assert (await RecoveryService(sessions).recover(session_id)).changed is False


async def test_duplicate_context_is_rejected_and_raw_duplicate_is_detected(tmp_path):
    sessions, session_id, _, snapshot = await opened(tmp_path)
    first = await sessions.append_context_input(
        session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
    )
    with pytest.raises(ValueError, match="context-input-binding-mismatch"):
        await sessions.append_context_input(session_id, snapshot.to_dict(), expected_seq=first.seq)
    await sessions.store.append(
        sessions.session_stream(session_id),
        expected_seq=first.seq,
        events=(
            PendingEvent(
                type="context/input",
                data=first.data,
                composition_revision=first.composition_revision,
            ),
        ),
    )
    events = await sessions.read_session(session_id)
    assert "context-input-binding" in {issue.name for issue in CoreInvariantChecker().check(events)}


async def test_competing_context_writer_must_not_rebase_old_frozen_input(tmp_path):
    sessions, session_id, _, snapshot = await opened(tmp_path)
    another = SessionService(sessions.store)
    results = await asyncio.gather(
        sessions.append_context_input(
            session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
        ),
        another.append_context_input(
            session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert any(
        isinstance(result, ConcurrencyConflict | ContextInputWriteError) for result in results
    )
    events = await sessions.read_session(session_id)
    assert sum(event.type == "context/input" for event in events) == 1


class ContextAppendFaultStore(InMemoryEventStore):
    def __init__(self, *, committed: bool, unknown: bool = False, gated: bool = False):
        super().__init__()
        self.committed = committed
        self.unknown = unknown
        self.gated = gated
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.faulted = False
        self.read_before_append_converged = False

    async def append(self, stream_id, *, expected_seq, events, **kwargs):
        if events[0].type != "context/input":
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )
        if self.committed:
            await super().append(stream_id, expected_seq=expected_seq, events=events, **kwargs)
        self.entered.set()
        if self.gated:
            await self.release.wait()
        self.faulted = True
        raise OSError("append acknowledgement failed")

    async def read(self, stream_id, *, from_seq=1):
        if self.entered.is_set() and not self.faulted:
            self.read_before_append_converged = True
        if self.faulted and self.unknown:
            raise OSError("reconciliation read failed")
        return await super().read(stream_id, from_seq=from_seq)


@pytest.mark.parametrize(
    "committed,unknown,expected", [(False, False, False), (True, False, True), (True, True, None)]
)
async def test_context_append_failure_preserves_three_commit_outcomes(
    tmp_path, committed, unknown, expected
):
    store = ContextAppendFaultStore(committed=committed, unknown=unknown)
    sessions, session_id, _, snapshot = await opened(tmp_path, store=store)
    with pytest.raises(ContextInputWriteError) as caught:
        await sessions.append_context_input(
            session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
        )
    assert store.entered.is_set()
    assert caught.value.committed is expected
    store.unknown = False
    events = await sessions.read_session(session_id)
    assert sum(event.type == "context/input" for event in events) == (1 if committed else 0)


@pytest.mark.parametrize("committed", [False, True])
async def test_repeated_cancel_waits_for_context_append_and_preserves_commit_evidence(
    tmp_path, committed
):
    store = ContextAppendFaultStore(committed=committed, gated=True)
    sessions, session_id, _, snapshot = await opened(tmp_path, store=store)
    append_task = asyncio.create_task(
        sessions.append_context_input(
            session_id, snapshot.to_dict(), expected_seq=snapshot.to_dict()["observed_session_seq"]
        )
    )
    await store.entered.wait()
    append_task.cancel()
    # A call_soon gate lets the cancellation handler run without guessed sleeps.
    cycle = asyncio.Event()
    asyncio.get_running_loop().call_soon(cycle.set)
    await cycle.wait()
    append_task.cancel()
    assert not append_task.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await append_task
    assert isinstance(caught.value.__cause__, ContextInputWriteError)
    assert caught.value.__cause__.committed is committed
    assert store.read_before_append_converged is False
    events = await sessions.read_session(session_id)
    assert sum(event.type == "context/input" for event in events) == (1 if committed else 0)
    assert not any(event.type == "composition/snapshot" for event in events)
