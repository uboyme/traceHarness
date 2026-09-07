"""One-Step Skill disclosure receipts on the existing Tool/Session lifecycle."""

from __future__ import annotations

from traceh.api.json_types import canonical_json, fingerprint
from traceh.kernel.composition import CompositionSnapshot
from traceh.session.history_requests import _context_for_request, _step, event_ref
from traceh.session.skill_selection import eligible_skills, project_selection

SKILL_TOOL_NAME = "request_skill_reference"
REQUEST_KEYS = {
    "skill_id",
    "version",
    "catalog_digest",
    "requested_tier",
    "section_id",
    "resource_id",
    "chunk_id",
}


def parse_request(data):
    if type(data) is not dict or set(data) != REQUEST_KEYS:
        raise ValueError("skill-request-invalid")
    if any(
        type(data[k]) is not str or not data[k]
        for k in ("skill_id", "version", "catalog_digest", "requested_tier")
    ):
        raise ValueError("skill-request-invalid")
    for key in ("section_id", "resource_id", "chunk_id"):
        if data[key] is not None and (type(data[key]) is not str or not data[key]):
            raise ValueError("skill-request-invalid")
    tier = data["requested_tier"]
    fields = {key for key in ("section_id", "resource_id", "chunk_id") if data[key] is not None}
    required = {
        "directory": set(),
        "summary": set(),
        "section": {"section_id"},
        "chunk": {"resource_id", "chunk_id"},
    }
    if tier not in required or fields != required[tier]:
        raise ValueError("skill-request-invalid")
    return data


def source_for_request(events, *, session_id, turn_id, step_id, tool_call_id, request, policy):
    from traceh.session.skill_retrieval import verify_block

    step, _ = _step(events, turn_id, step_id)
    if any(e.type == "runtime/cancel-requested" for e in events[step.seq :]):
        raise ValueError("skill-request-expired")
    calls = [
        e
        for e in events[step.seq :]
        if e.type == "tool/call" and e.data.get("tool_name") == SKILL_TOOL_NAME
    ]
    if len(calls) > policy.max_requests:
        raise ValueError("skill-request-resource-limit")
    matches = [e for e in calls if e.data.get("tool_call_id") == tool_call_id]
    if len(matches) != 1 or canonical_json(matches[0].data.get("arguments")) != canonical_json(
        request
    ):
        raise ValueError("skill-request-source-invalid")
    call = matches[0]
    if call.data.get("turn_id") != turn_id or call.data.get("step_id") != step_id:
        raise ValueError("skill-request-source-invalid")
    snapshots = [e for e in events[step.seq : call.seq - 1] if e.type == "request/snapshot"]
    if len(snapshots) != 1:
        raise ValueError("skill-request-not-disclosed")
    snapshot = snapshots[0]
    composition = CompositionSnapshot.from_dict(events[snapshot.data["source_seq"] - 1].data)
    context = _context_for_request(events, snapshot)
    context_event = events[snapshot.data["context_input_seq"] - 1]
    expected_call = {"id": tool_call_id, "name": SKILL_TOOL_NAME, "arguments": request}
    if (
        not any(
            expected_call in e.data.get("tool_calls", [])
            for e in events[snapshot.seq : call.seq - 1]
            if e.type == "assistant/message"
        )
        or not any(t.name == SKILL_TOOL_NAME for t in composition.tools)
        or call.composition_revision != composition.revision
        or request["catalog_digest"] != composition.skill_catalog_digest
        or not any(
            b["kind"] == "skill"
            and b["id"] == request["skill_id"]
            and b["version"] == request["version"]
            for b in context["blocks"]
        )
        or context["policy"]["config"]["skills"] != policy.to_dict()
    ):
        raise ValueError("skill-request-not-disclosed")
    descriptor = next(s for s in composition.skill_catalog if s.skill_id == request["skill_id"])
    for block in context["blocks"]:
        if block["kind"] == "skill" and block["id"] == descriptor.skill_id:
            verify_block(block, descriptor, composition.skill_catalog_digest)
    tier = request["requested_tier"]
    if tier == "section" and not any(
        s.section_id == request["section_id"] for s in descriptor.sections
    ):
        raise ValueError("skill-request-source-invalid")
    if tier == "chunk" and not any(
        r.resource_id == request["resource_id"]
        and any(c.chunk_id == request["chunk_id"] for c in r.chunks)
        for r in descriptor.resources
    ):
        raise ValueError("skill-request-source-invalid")
    return descriptor, context_event, composition


def receipt_for(events, *, context, request, policy, selections):
    request = parse_request(request)
    descriptor, source, composition = source_for_request(
        events,
        session_id=context.session_id,
        turn_id=context.turn_id,
        step_id=context.step_id,
        tool_call_id=context.tool_call_id,
        request=request,
        policy=policy,
    )
    eligible, _ = eligible_skills(project_selection(selections, context.session_id), composition)
    if descriptor not in eligible:
        raise ValueError("skill-request-not-selected")
    receipt = {
        "format": 1,
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "source_step_id": context.step_id,
        "tool_call_id": context.tool_call_id,
        "context_ref": event_ref(source),
        "request": request,
        "policy_digest": policy.digest,
        "target_rule": "immediate-next-step",
    }
    available = {
        "sections": [s.section_id for s in descriptor.sections],
        "resources": [
            {"resource_id": r.resource_id, "chunks": [c.chunk_id for c in r.chunks]}
            for r in descriptor.resources
        ],
    }
    return receipt, available


def eligible_requests(events, *, session_id, turn_id, step_id, policy):
    step, starts = _step(events, turn_id, step_id)
    if len(starts) < 2 or any(
        e.type == "runtime/cancel-requested" for e in events[starts[0].seq :]
    ):
        return ()
    previous = starts[-2]
    ends = [e for e in events[previous.seq : step.seq - 1] if e.type == "step/end"]
    if len(ends) != 1 or ends[0].data.get("reason") != "model_response":
        return ()
    result, seen = [], set()
    for event in events[previous.seq : ends[0].seq - 1]:
        if event.type != "tool/result" or event.data.get("error_type") == "RecoveredAfterCrash":
            continue
        data = event.data.get("data")
        if not isinstance(data, dict) or "skill_receipt" not in data:
            continue
        if event.data.get("status") != "succeeded":
            raise ValueError("skill-receipt-not-successful")
        receipt = data["skill_receipt"]
        if type(receipt) is not dict or set(receipt) != {
            "format",
            "session_id",
            "turn_id",
            "source_step_id",
            "tool_call_id",
            "context_ref",
            "request",
            "policy_digest",
            "target_rule",
        }:
            raise ValueError("skill-receipt-invalid")
        if (
            type(receipt["format"]) is not int
            or receipt["format"] != 1
            or receipt["session_id"] != session_id
            or receipt["turn_id"] != turn_id
            or receipt["source_step_id"] != previous.data["step_id"]
            or receipt["tool_call_id"] != event.data.get("tool_call_id")
            or event.data.get("tool_name") != SKILL_TOOL_NAME
            or receipt["policy_digest"] != policy.digest
            or receipt["target_rule"] != "immediate-next-step"
        ):
            raise ValueError("skill-receipt-binding-mismatch")
        request = parse_request(receipt["request"])
        _, source, _ = source_for_request(
            events[: event.seq - 1],
            session_id=session_id,
            turn_id=turn_id,
            step_id=previous.data["step_id"],
            tool_call_id=receipt["tool_call_id"],
            request=request,
            policy=policy,
        )
        if receipt["context_ref"] != event_ref(source):
            raise ValueError("skill-receipt-binding-mismatch")
        key = fingerprint(request)
        if key not in seen:
            seen.add(key)
            result.append(request)
    if len(result) > policy.max_requests:
        raise ValueError("skill-request-resource-limit")
    return tuple(result)
