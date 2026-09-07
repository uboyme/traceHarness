"""Original Product owner evidence and explicit policy/ownership refusal."""

import pytest
from memory_fixtures import approve, memory_policy, setup
from product_fixtures import ORIGIN_SESSION, build_assembly, opened, seed_session

from traceh.api.memory import MemoryPolicy
from traceh.memory.service import MemoryService
from traceh.projects.events import reference
from traceh.projects.service import ProjectScopeService
from traceh.runtime.memory_control import MemoryControl
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


async def test_product_evidence_comes_from_original_task_and_rejects_transient_facts(tmp_path):
    authority, scope, sessions, store, _ = await setup(tmp_path, session_id=ORIGIN_SESSION)
    seeded = InMemoryEventStore()
    await seed_session(seeded)
    # Real Session identity/path was created by SessionService; the shared fixture
    # contributes only the normal accepted/claimed/closed confirmation sequence.
    for event in (await seeded.read(f"session:{ORIGIN_SESSION}"))[1:]:
        await sessions.append_session(ORIGIN_SESSION, event.type, event.data)
    assembly = await build_assembly(store=store, seed=False)
    try:
        await opened(assembly)
        await assembly.service.cancel_task(
            task_id="task-1", operation_id="cancel", reason_code="user-requested"
        )
        events = await store.read("product-task:task-1")
        source = {
            "kind": "product-evidence",
            "requester_binding_ref": reference(await scope.resolve(ORIGIN_SESSION)),
            "task_id": "task-1",
            "event_refs": [reference(events[-1])],
            "observed_head": len(events),
        }
        proposal = await authority.propose(
            ORIGIN_SESSION,
            proposal_id="previous-plan",
            body="The previous plan was cancelled by the user.",
            sources=[source],
            operation_id="previous-plan",
            actor_id="operator",
            expected_head=0,
        )
        await approve(authority, proposal, session_id=ORIGIN_SESSION)
        assert (await authority.read(ORIGIN_SESSION)).active[0].memory_id == "memory-goal"
        for changed in (
            {**source, "event_refs": [reference(events[0])]},
            {**source, "task_id": "missing-task"},
            {**source, "event_refs": [{**reference(events[-1]), "digest": "a" * 64}]},
        ):
            with pytest.raises(ValueError):
                await authority.propose(
                    ORIGIN_SESSION,
                    proposal_id="bad-evidence",
                    body="Unproven claim.",
                    sources=[changed],
                    operation_id="bad-evidence",
                    actor_id="operator",
                    expected_head=2,
                )
        assert await store.head("memory:project-orion") == 2
    finally:
        await assembly.aclose()


@pytest.mark.parametrize(
    "field",
    ["max_body_bytes", "max_sources", "max_source_events", "max_source_bytes", "max_memory_events"],
)
def test_each_authority_limit_must_be_explicit_positive_integer(field):
    for value in (0, -1, True, None, 1.5):
        with pytest.raises(ValueError):
            memory_policy(**{field: value})


def test_missing_content_rules_or_malformed_regex_refuses_assembly():
    for value in ((), ("",), ("[",), ["pattern"]):
        with pytest.raises(ValueError):
            memory_policy(denied_patterns=value)
    with pytest.raises(TypeError):
        MemoryPolicy(max_body_bytes=100)


@pytest.mark.parametrize("value", [b"not-json", {"nested": object()}, {"set"}])
async def test_non_json_control_payload_is_rejected_with_zero_append(tmp_path, value):
    authority, _, _, store, _ = await setup(tmp_path)
    with pytest.raises(ValueError):
        await authority.propose(
            "requester",
            proposal_id="invalid",
            body="Candidate.",
            sources=value,
            operation_id="invalid",
            actor_id="operator",
            expected_head=0,
        )
    assert await store.head("memory:project-orion") == 0


async def test_memory_control_requires_exact_session_owner_even_on_same_store(tmp_path):
    from test_memory_runtime import build

    from traceh.api.llm import ModelResponse
    from traceh.llm.scripted import ScriptedLlmProvider

    runtime, _, session = await build(
        tmp_path, ScriptedLlmProvider((ModelResponse(content="done"),))
    )
    configured = runtime.config.memory
    foreign = MemoryService(
        ProjectScopeService(
            SessionService(runtime.sessions.store),
            configured.source_resolver,
            configured.project_limits,
        ),
        configured.memory_policy,
    )
    control = MemoryControl(runtime.sessions, runtime.loop.compositions, foreign)
    try:
        with pytest.raises(ValueError, match="memory-context-owner-mismatch"):
            await control.read(session)
    finally:
        await runtime.dispose()
