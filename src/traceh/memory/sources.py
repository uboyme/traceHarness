"""Fresh proof of bounded immutable evidence, with no cross-Session disclosure API."""

from traceh.api.json_types import canonical_json
from traceh.memory.policy import content, source_shapes
from traceh.product.events import product_task_stream
from traceh.product.projection import rebuild_product_task
from traceh.projects.events import reference, referenced
from traceh.session.history import closed_turn_membership
from traceh.session.surface_replacement import SURFACE_MESSAGE_TYPES

PRODUCT_LEAVES = frozenset(
    {
        "product/task-completed",
        "product/task-failed",
        "product/task-cancelled",
        "product/task-rejected",
        "product/task-abandoned",
    }
)
PRIVATE_KEYS = frozenset(
    {
        "credentials",
        "api_key",
        "access_token",
        "approval_token",
        "approval_capability",
        "evaluator_input",
        "verification_plan",
        "budget_credentials",
        "frozen_input",
    }
)


def _private(value, policy):
    if isinstance(value, dict):
        keys = {key.casefold() for key in value}
        return bool(PRIVATE_KEYS.intersection(keys)) or any(
            _private(v, policy) for v in value.values()
        )
    if isinstance(value, list):
        return any(_private(v, policy) for v in value)
    if isinstance(value, str) and value.strip():
        # Inspect original strings too: serializing a JSON-looking message escapes
        # its quotes and must not hide a recognized credential assignment.
        content(value, policy, limit=policy.max_source_bytes)
    return False


async def validate_sources(scope, project_id, sources, policy):
    source_shapes(sources, policy)
    catalog = await scope.catalog()
    total_bytes = 0
    for source in sources:
        if source["kind"] == "host-declaration":
            continue
        session_source = source["kind"] == "session-evidence"
        binding_ref = source["binding_ref" if session_source else "requester_binding_ref"]
        binding = referenced(catalog.events, binding_ref)
        if binding.type != "project/session-bound" or binding.data["project_id"] != project_id:
            raise ValueError("memory-source-project-mismatch")
        session_id = binding.data["session_ref"]["stream_id"].removeprefix("session:")
        if reference(await scope.resolve_evidence(session_id)) != binding_ref:
            raise ValueError("memory-source-binding-mismatch")
        if session_source:
            events = await scope.sessions.read_session(session_id)
        else:
            events = await scope.store.read(product_task_stream(source["task_id"]))
            task = rebuild_product_task(source["task_id"], events)
            if (
                task is None
                or task.origin_session_id != session_id
                or task.confirmation_session_id != session_id
            ):
                raise ValueError("memory-product-requester-mismatch")
        head = source["observed_head"]
        if head > len(events) or head > policy.max_source_events:
            raise ValueError("memory-source-head-invalid")
        prefix = events[:head]
        total_bytes += sum(len(canonical_json(e.to_dict()).encode("utf-8")) for e in prefix)
        if total_bytes > policy.max_source_bytes:
            raise ValueError("memory-source-limit")
        if session_source:
            if prefix[-1].type != "turn/end":
                raise ValueError("memory-source-not-closed")
            closed = closed_turn_membership(prefix)
        for ref in source["event_refs"]:
            event = referenced(prefix, ref)
            if session_source:
                if event.type not in SURFACE_MESSAGE_TYPES or event.seq not in closed:
                    raise ValueError("memory-source-leaf-invalid")
            elif event.type not in PRODUCT_LEAVES:
                raise ValueError("memory-source-leaf-invalid")
            if _private(event.data, policy):
                raise ValueError("memory-source-private")
            content(canonical_json(event.data), policy, limit=policy.max_source_bytes)


async def session_source(scope, session_id, event_ids, policy):
    """Resolve explicit original leaf IDs of this Session into a closed prefix."""
    if type(event_ids) is not list or not event_ids or len(event_ids) > policy.max_source_events:
        raise ValueError("memory-source-count-invalid")
    if any(type(value) is not str for value in event_ids) or len(set(event_ids)) != len(event_ids):
        raise ValueError("memory-source-ref-invalid")
    binding = await scope.resolve(session_id)
    events = await scope.sessions.read_session(session_id)
    if len(events) > policy.max_source_events:
        raise ValueError("memory-source-limit")
    closed = closed_turn_membership(events)
    selected = [e for e in events if str(e.event_id) in event_ids]
    if len(selected) != len(event_ids) or any(e.seq not in closed for e in selected):
        raise ValueError("memory-source-not-closed")
    return {
        "kind": "session-evidence",
        "binding_ref": reference(binding),
        "event_refs": [reference(e) for e in selected],
        "observed_head": max(closed[e.seq] for e in selected),
    }
