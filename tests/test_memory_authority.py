"""Public authority transitions, exact references, evidence and refusal paths."""

from dataclasses import replace

import pytest
from memory_fixtures import approve, bind, closed_source, declare, memory_policy, setup

from traceh.api.events import PendingEvent
from traceh.api.json_types import fingerprint
from traceh.memory.projection import replay_memory
from traceh.memory.service import MemoryService
from traceh.memory.sources import session_source
from traceh.projects.events import reference
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.sqlite import SqliteEventStore


async def test_exact_transitions_detached_views_and_historical_reconstruction(tmp_path):
    authority, _, _, store, _ = await setup(tmp_path)
    proposal = await declare(authority)
    assert not (await authority.read("requester")).active
    activation = await approve(authority, proposal)
    old = await authority.read("requester")
    new = await declare(
        authority, proposal_id="proposal-plan", body="The approved phase is integration."
    )
    successor = await authority.supersede(
        "requester",
        proposal_ref=reference(new),
        proposal_digest=new.data["proposal_digest"],
        memory_id="memory-plan",
        fact_slot="goal",
        predecessor_ref=reference(activation),
        predecessor_digest=proposal.data["proposal_digest"],
        operation_id="replace-goal",
        actor_id="operator",
        expected_head=3,
    )
    assert [f.memory_id for f in (await authority.read("requester")).active] == ["memory-plan"]
    assert [
        f.memory_id for f in replay_memory(old.project_id, old.events, authority.policy).active
    ] == ["memory-goal"]
    old.active[0].proposal.data["body"] = "tampered"
    assert (await store.read("memory:project-orion"))[0].data["body"] == proposal.data["body"]
    await authority.revoke(
        "requester",
        memory_id="memory-plan",
        fact_slot="goal",
        predecessor_ref=reference(successor),
        predecessor_digest=new.data["proposal_digest"],
        operation_id="revoke-goal",
        actor_id="operator",
        expected_head=4,
    )
    assert not (await authority.read("requester")).active
    assert await store.head("memory:project-orion") == 5
    with pytest.raises(ValueError, match="memory-approval-mismatch"):
        await approve(authority, new, memory_id="revive-consumed")


async def test_same_operation_exact_receipt_and_conflicting_content(tmp_path):
    authority, _, _, store, _ = await setup(tmp_path)
    proposal = await declare(authority)
    activation = await approve(authority, proposal)
    assert await approve(authority, proposal, expected_head=1) == activation
    with pytest.raises(ValueError, match="operation-conflict"):
        await approve(authority, proposal, expected_head=1, fact_slot="different")
    with pytest.raises(ValueError):
        await declare(authority, body="Changed body under the same identity.")
    assert await store.head("memory:project-orion") == 2


@pytest.mark.parametrize("variant", ["digest", "envelope", "head", "project", "proposal-type"])
async def test_wrong_approval_reference_is_rejected_without_append(tmp_path, variant):
    authority, _, _, store, _ = await setup(tmp_path)
    proposal = await declare(authority)
    changes = {}
    if variant == "digest":
        changes["proposal_digest"] = "a" * 64
    elif variant == "envelope":
        changes["proposal_ref"] = {**reference(proposal), "digest": "b" * 64}
    elif variant == "head":
        changes["expected_head"] = 0
    elif variant == "project":
        changes["proposal_ref"] = {**reference(proposal), "stream_id": "memory:foreign"}
    else:
        changes["proposal_ref"] = {**reference(proposal), "type": "memory/approved"}
    with pytest.raises((ValueError, ConcurrencyConflict)):
        await approve(authority, proposal, **changes)
    assert await store.head("memory:project-orion") == 1


@pytest.mark.parametrize(
    "variant", ["occupied", "consumed", "duplicate-id", "predecessor", "stale-head"]
)
async def test_slot_consumption_and_exact_predecessor_refusal(tmp_path, variant):
    authority, _, _, store, _ = await setup(tmp_path)
    first = await declare(authority)
    activation = await approve(authority, first)
    second = await declare(authority, proposal_id="proposal-other")
    before = await store.head("memory:project-orion")
    with pytest.raises((ValueError, ConcurrencyConflict)):
        if variant == "occupied":
            await approve(authority, second, memory_id="different")
        elif variant == "consumed":
            await approve(authority, first, memory_id="different", fact_slot="other")
        elif variant == "duplicate-id":
            await approve(authority, second, fact_slot="other", operation_id="different-operation")
        else:
            await authority.revoke(
                "requester",
                memory_id="memory-goal",
                fact_slot="goal",
                predecessor_ref=reference(activation),
                predecessor_digest="c" * 64
                if variant == "predecessor"
                else first.data["proposal_digest"],
                operation_id="revoke",
                actor_id="operator",
                expected_head=2 if variant == "stale-head" else 3,
            )
    assert await store.head("memory:project-orion") == before


@pytest.mark.parametrize(
    "body",
    [
        ".env contents",
        "api_key=synthetic-rejected-value",
        '{"API_KEY":"synthetic-rejected-value"}',
        "-----BEGIN PRIVATE KEY-----",
        "currently editing the module",
        "正在等待批准",
        "fixture-private-input",
        "x" * 1025,
    ],
)
async def test_content_is_rejected_before_memory_append(tmp_path, body):
    authority, _, _, store, _ = await setup(tmp_path)
    with pytest.raises(ValueError, match="memory-content"):
        await declare(authority, body=body)
    assert await store.head("memory:project-orion") == 0


@pytest.mark.parametrize(
    "body",
    [
        "Long-term goal: auditable decisions.",
        "Constraint: preserve append-only history.",
        "Term: a project is a durable host association.",
        "Decision: use one EventStore.",
        "Milestone completed: ownership review.",
        "当前获批阶段为集成验证。",
        "批准的后续路线为文档和发布检查。",
    ],
)
async def test_stable_fact_categories_remain_usable(tmp_path, body):
    authority, _, _, _, _ = await setup(tmp_path)
    proposal = await declare(authority, body=body)
    await approve(authority, proposal)
    assert (await authority.read("requester")).active[0].body == body


async def test_model_sources_are_closed_current_session_evidence_and_not_declarations(tmp_path):
    authority, scope, sessions, store, _ = await setup(tmp_path)
    leaf = await closed_source(sessions)
    proposal = await authority.propose_model(
        "requester",
        proposal_id="model-proposal",
        body="A stable goal.",
        source_event_ids=[str(leaf.event_id)],
        operation_id="model-call",
        actor_id="model-test",
    )
    assert not (await authority.read("requester")).active
    assert proposal.data["sources"][0]["kind"] == "session-evidence"
    await approve(authority, proposal)
    await sessions.create_session(
        await sessions.workspace_for("requester"), session_id="next-session"
    )
    await bind(scope, "next-session")
    assert (await authority.read("next-session")).active[0].memory_id == "memory-goal"
    with pytest.raises(ValueError, match="not-closed"):
        await authority.propose_model(
            "next-session",
            proposal_id="steal",
            body="A stable goal.",
            source_event_ids=[str(leaf.event_id)],
            operation_id="steal",
            actor_id="model-test",
        )
    with pytest.raises(ValueError, match="host-only"):
        await authority.propose(
            "requester",
            proposal_id="forge",
            body="Forged claim.",
            sources=[{"kind": "host-declaration"}],
            operation_id="forge",
            actor_id="model-test",
            expected_head=2,
        )
    assert await store.head("memory:project-orion") == 2


@pytest.mark.parametrize(
    "variant",
    [
        "ref",
        "head",
        "private",
        "quoted-private",
        "open",
        "other-project",
        "audit",
    ],
)
async def test_invalid_source_never_becomes_a_proposal(tmp_path, variant):
    authority, scope, sessions, store, resolver = await setup(tmp_path)
    body = "api_key=synthetic-private-input" if variant == "private" else "A stable requirement."
    if variant == "quoted-private":
        body = '{"API_KEY":"synthetic-private-input"}'
    leaf = await closed_source(sessions, body=body)
    source = await session_source(scope, "requester", [str(leaf.event_id)], authority.policy)
    if variant == "ref":
        source["event_refs"][0]["digest"] = "d" * 64
    elif variant == "head":
        source["observed_head"] += 10
    elif variant == "open":
        source["observed_head"] -= 1
    elif variant == "other-project":
        root = tmp_path / "other"
        root.mkdir()
        resolver.mappings["source-other"] = root
        await sessions.create_session(root, session_id="foreign")
        await bind(scope, "foreign", project_id="project-other", source_id="source-other")
        source["binding_ref"] = reference(await scope.resolve("foreign"))
    elif variant == "audit":
        source["event_refs"] = [reference((await sessions.read_session("requester"))[1])]
    with pytest.raises(ValueError):
        await authority.propose(
            "requester",
            proposal_id="bad-source",
            body="A stable goal.",
            sources=[source],
            operation_id="bad-source",
            actor_id="operator",
            expected_head=0,
        )
    assert await store.head("memory:project-orion") == 0


@pytest.mark.parametrize(
    "variant", ["unknown-type", "schema", "format", "order", "extra", "duplicate"]
)
async def test_damaged_stream_fails_closed_on_fresh_read(tmp_path, variant):
    authority, _, _, store, _ = await setup(tmp_path)
    proposal = await declare(authority)
    if variant == "unknown-type":
        pending = PendingEvent("memory/unknown", proposal.data, actor_id="operator")
    else:
        data = {
            **proposal.data,
            "operation_id": "second",
            "expected_head": 1,
            "proposal_id": "second",
        }
        if variant == "format":
            data["format"] = 2
        if variant == "extra":
            data["active"] = True
        if variant == "duplicate":
            data["operation_id"] = proposal.data["operation_id"]
        data["proposal_digest"] = fingerprint(
            {k: data[k] for k in ("project_id", "proposal_id", "body", "sources")}
        )
        pending = PendingEvent(
            "memory/proposed",
            data,
            schema_version=2 if variant == "schema" else 1,
            actor_id="operator",
        )
    if variant == "order":
        with pytest.raises(ValueError):
            replay_memory("project-orion", (replace(proposal, seq=2),), authority.policy)
        return
    await store.append("memory:project-orion", expected_seq=1, events=(pending,))
    with pytest.raises(ValueError):
        await authority.read("requester")


async def test_reopen_same_sqlite_uses_only_original_canonical_streams(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = SqliteEventStore(path)
    authority, scope, sessions, _, resolver = await setup(tmp_path, store=store)
    proposal = await declare(authority)
    await approve(authority, proposal)
    await store.aclose()
    reopened = SqliteEventStore(path)
    try:
        from traceh.projects.service import ProjectScopeService
        from traceh.session.service import SessionService

        fresh = MemoryService(
            ProjectScopeService(SessionService(reopened), resolver, scope.limits), memory_policy()
        )
        assert (await fresh.read("requester")).active[0].body == proposal.data["body"]
        assert set(await reopened.list_streams()) == {
            "session:requester",
            "projects:catalog",
            "memory:project-orion",
        }
    finally:
        await reopened.aclose()


async def test_maximum_opaque_identity_is_valid_through_approval(tmp_path):
    authority, _, _, _, _ = await setup(tmp_path, project_id="p" * 256, session_id="s" * 256)
    proposal = await declare(authority, session_id="s" * 256)
    await approve(authority, proposal, session_id="s" * 256)
    assert len((await authority.read("s" * 256)).active) == 1
