"""Deterministic public CAS, cancellation and true/false/unknown append reconciliation."""

import asyncio

import pytest
from memory_fixtures import approve, declare, setup

from traceh.projects.events import AuthorityWriteError, reference
from traceh.session.event_store import InMemoryEventStore


class ControlledStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.kind = None
        self.failure = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.arrivals = 0
        self.unreadable = False
        self.uncertain_stream = None
        self.finished = asyncio.Event()

    async def append(self, stream_id, *, expected_seq, events, **kwargs):
        if events[0].type != self.kind:
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )
        self.arrivals += 1
        self.entered.set()
        if self.arrivals == 2:
            self.second_entered.set()
        await self.release.wait()
        try:
            if self.failure == "before":
                raise RuntimeError("injected-before-commit")
            result = await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )
            if self.failure in ("after", "unknown"):
                self.unreadable = self.failure == "unknown"
                self.uncertain_stream = stream_id
                raise RuntimeError("injected-after-commit")
            return result
        finally:
            self.finished.set()

    async def read(self, stream_id, *, from_seq=1):
        if self.unreadable and stream_id == self.uncertain_stream:
            raise RuntimeError("injected-unreadable")
        return await super().read(stream_id, from_seq=from_seq)


async def test_read_rechecks_head_after_source_proof(tmp_path):
    class ReadGateStore(InMemoryEventStore):
        enabled = False

        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def read(self, stream_id, *, from_seq=1):
            result = await super().read(stream_id, from_seq=from_seq)
            if self.enabled and stream_id.startswith("memory:"):
                self.enabled = False
                self.entered.set()
                await self.release.wait()
            return result

    store = ReadGateStore()
    authority, _, _, _, _ = await setup(tmp_path, store=store)
    await declare(authority)
    store.enabled = True
    read = asyncio.create_task(authority.read("requester"))
    await store.entered.wait()
    await authority.declare(
        "requester",
        proposal_id="another",
        body="Another approved direction.",
        statement="Another approved direction.",
        declaration_id="another-declaration",
        operation_id="another",
        actor_id="operator",
        expected_head=1,
    )
    store.release.set()
    with pytest.raises(ValueError, match="memory-source-unavailable"):
        await read
    assert (await authority.read("requester")).head == 2


async def test_two_proposals_cannot_activate_one_slot_concurrently(tmp_path):
    store = ControlledStore()
    authority, _, _, _, _ = await setup(tmp_path, store=store)
    one = await declare(authority)
    two = await declare(authority, proposal_id="proposal-two")
    store.kind = "memory/approved"
    first = asyncio.create_task(approve(authority, one, memory_id="first", expected_head=2))
    second = asyncio.create_task(approve(authority, two, memory_id="second", expected_head=2))
    await store.second_entered.wait()
    store.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(isinstance(r, AuthorityWriteError) for r in results) == 1
    assert len((await authority.read("requester")).active) == 1
    assert await store.head("memory:project-orion") == 3


async def test_concurrent_identical_operation_returns_one_receipt(tmp_path):
    store = ControlledStore()
    authority, _, _, _, _ = await setup(tmp_path, store=store)
    proposal = await declare(authority)
    store.kind = "memory/approved"
    first = asyncio.create_task(approve(authority, proposal, expected_head=1))
    second = asyncio.create_task(approve(authority, proposal, expected_head=1))
    await store.second_entered.wait()
    store.release.set()
    receipts = await asyncio.gather(first, second)
    assert receipts[0] == receipts[1]
    assert await store.head("memory:project-orion") == 2


@pytest.mark.parametrize(
    "kind",
    [
        "memory/proposed",
        "memory/approved",
        "memory/superseded",
        "memory/revoked",
        "project/session-bound",
    ],
)
@pytest.mark.parametrize(
    "failure,committed", [("before", False), ("after", True), ("unknown", None)]
)
async def test_failed_write_reconciles_exact_public_operation(tmp_path, kind, failure, committed):
    store = ControlledStore()
    authority, scope, sessions, _, _ = await setup(tmp_path, store=store)
    proposal = (
        await declare(authority)
        if kind
        in {
            "memory/approved",
            "memory/superseded",
            "memory/revoked",
        }
        else None
    )
    predecessor = None
    replacement = None
    if kind in {"memory/superseded", "memory/revoked"}:
        predecessor = await approve(authority, proposal)
    if kind == "memory/superseded":
        replacement = await declare(authority, proposal_id="replacement")
    if kind == "project/session-bound":
        await sessions.create_session(
            await sessions.workspace_for("requester"), session_id="new-session"
        )
    store.kind = kind
    store.failure = failure
    store.release.set()
    target = "projects:catalog" if kind.startswith("project/") else "memory:project-orion"
    before = await store.head(target)
    if kind == "memory/approved":
        operation = approve(authority, proposal)
    elif kind == "memory/proposed":
        operation = declare(authority)
    elif kind in {"memory/superseded", "memory/revoked"}:
        fields = {
            "memory_id": "replacement" if replacement else "memory-goal",
            "fact_slot": "goal",
            "predecessor_ref": reference(predecessor),
            "predecessor_digest": proposal.data["proposal_digest"],
            "operation_id": "decision",
            "actor_id": "operator",
            "expected_head": before,
        }
        if replacement:
            operation = authority.supersede(
                "requester",
                **fields,
                proposal_ref=reference(replacement),
                proposal_digest=replacement.data["proposal_digest"],
            )
        else:
            operation = authority.revoke("requester", **fields)
    else:
        operation = scope.bind_session(
            "new-session",
            project_id="project-orion",
            operation_id="new-bind",
            actor_id="operator",
            expected_head=3,
        )
    with pytest.raises(AuthorityWriteError) as error:
        await operation
    assert error.value.committed is committed
    assert store.arrivals == 1 and store.finished.is_set()
    store.unreadable = False
    assert await store.head(target) == before + (failure != "before")


@pytest.mark.parametrize("failure,committed", [(None, True), ("before", False), ("unknown", None)])
async def test_repeated_cancellation_waits_for_append_and_reconciliation(
    tmp_path, failure, committed
):
    store = ControlledStore()
    authority, _, _, _, _ = await setup(tmp_path, store=store)
    proposal = await declare(authority)
    store.kind = "memory/approved"
    store.failure = failure
    call = asyncio.create_task(approve(authority, proposal))
    await store.entered.wait()
    call.cancel()
    call.cancel()
    assert not call.done() and not store.finished.is_set()
    store.release.set()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await call
    assert isinstance(cancellation.value.__cause__, AuthorityWriteError)
    assert cancellation.value.__cause__.committed is committed
    assert store.finished.is_set() and store.arrivals == 1
    store.unreadable = False
    assert len((await authority.read("requester")).active) == (failure != "before")
