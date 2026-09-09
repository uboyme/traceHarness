"""Exact disclosed Memory requests, using the shared reference receipt lifetime."""

from traceh.api.json_types import canonical_json
from traceh.kernel.composition import CompositionSnapshot
from traceh.session.history_requests import _context_for_request, _step, event_ref
from traceh.session.reference_requests import RECEIPT_FORMAT, TARGET_RULE

MEMORY_TOOL_NAME = "request_workspace_memory"
REQUEST_KEYS = {"memory_id", "version", "requested_tier"}


def parse_request(data):
    if (
        type(data) is not dict
        or set(data) != REQUEST_KEYS
        or any(type(v) is not str or not v for v in data.values())
        or data["requested_tier"] not in {"directory", "summary", "section"}
    ):
        raise ValueError("memory-disclosure-not-authorized")
    return data


def source_for_request(events, *, session_id, turn_id, step_id, tool_call_id, request, policy):
    step, _ = _step(events, turn_id, step_id)
    if any(e.type == "runtime/cancel-requested" for e in events[step.seq :]):
        raise ValueError("memory-request-expired")
    calls = [
        e
        for e in events[step.seq :]
        if e.type == "tool/call" and e.data.get("tool_name") == MEMORY_TOOL_NAME
    ]
    if len(calls) > policy.max_requests:
        raise ValueError("memory-request-resource-limit")
    matches = [e for e in calls if e.data.get("tool_call_id") == tool_call_id]
    if len(matches) != 1 or canonical_json(matches[0].data.get("arguments")) != canonical_json(
        request
    ):
        raise ValueError("memory-request-source-invalid")
    call = matches[0]
    if call.data.get("turn_id") != turn_id or call.data.get("step_id") != step_id:
        raise ValueError("memory-request-source-invalid")
    snapshots = [e for e in events[step.seq : call.seq - 1] if e.type == "request/snapshot"]
    if len(snapshots) != 1:
        raise ValueError("memory-request-not-disclosed")
    snapshot = snapshots[0]
    composition = CompositionSnapshot.from_dict(events[snapshot.data["source_seq"] - 1].data)
    context = _context_for_request(events, snapshot)
    expected = {"id": tool_call_id, "name": MEMORY_TOOL_NAME, "arguments": request}
    blocks = [
        b
        for b in context["blocks"]
        if b["kind"] == "memory"
        and b["id"] == request["memory_id"]
        and b["version"] == request["version"]
    ]
    if not blocks:
        from traceh.session.context_input import _parse_policy
        from traceh.session.memory_search import disclosed

        blocks = disclosed(events, context, request, _parse_policy(context["policy"]))
    if (
        not any(
            expected in e.data.get("tool_calls", [])
            for e in events[snapshot.seq : call.seq - 1]
            if e.type == "assistant/message"
        )
        or not any(t.name == MEMORY_TOOL_NAME for t in composition.tools)
        or call.composition_revision != composition.revision
        or len(blocks) != 1
        or context["policy"]["config"]["memory"] != policy.to_dict()
    ):
        raise ValueError("memory-request-not-disclosed")
    return blocks[0], events[snapshot.data["context_input_seq"] - 1], composition


def eligible_requests(events, *, session_id, turn_id, step_id, policy):
    from traceh.session.reference_requests import eligible_requests as collect

    return collect(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        policy=policy,
        kind="memory",
        tool_name=MEMORY_TOOL_NAME,
        parse_request=parse_request,
        source_for_request=source_for_request,
        matches=matches_block,
    )


def receipt_for(events, *, context, request, policy, source):
    request = parse_request(request)
    block, event, _ = source_for_request(
        events,
        session_id=context.session_id,
        turn_id=context.turn_id,
        step_id=context.step_id,
        tool_call_id=context.tool_call_id,
        request=request,
        policy=policy,
    )
    fact = (
        next((fact for fact in source.view.active if fact.memory_id == request["memory_id"]), None)
        if source
        else None
    )
    if (
        fact is None
        or block["scope"] != source.scope
        or fact.proposal.data["proposal_digest"] != request["version"]
        or event_ref(fact.activation) != block["provenance"]["activation_ref"]
    ):
        raise ValueError("memory-request-not-active")
    return {
        "format": RECEIPT_FORMAT,
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "source_step_id": context.step_id,
        "tool_call_id": context.tool_call_id,
        "context_ref": event_ref(event),
        "request": request,
        "policy_digest": policy.digest,
        "target_rule": TARGET_RULE,
    }


def matches_block(request, block):
    return (
        block["kind"] == "memory"
        and request["memory_id"] == block["id"]
        and request["version"] == block["version"]
        and request["requested_tier"] == block["tier"]
    )
