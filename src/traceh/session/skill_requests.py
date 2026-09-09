"""Skill disclosure receipts on the shared bounded Turn lifecycle."""

from __future__ import annotations

from traceh.api.json_types import canonical_json
from traceh.kernel.composition import CompositionSnapshot
from traceh.session.history_requests import _context_for_request, _step, event_ref
from traceh.session.reference_requests import RECEIPT_FORMAT, TARGET_RULE
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
    if tier not in required:
        raise ValueError("skill-request-invalid")
    if fields != required[tier]:
        needed = ", ".join(sorted(required[tier])) or "none"
        unused = ", ".join(sorted({"section_id", "resource_id", "chunk_id"} - required[tier]))
        raise ValueError(
            f"skill-request-invalid: {tier} requires IDs {needed}; "
            f"{unused} must be JSON null (unquoted), not a string"
        )
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
    from traceh.session.context_input import _parse_policy
    from traceh.session.skill_search import disclosed

    search_grant = disclosed(events, context, request, _parse_policy(context["policy"]))
    if (
        not any(
            expected_call in e.data.get("tool_calls", [])
            for e in events[snapshot.seq : call.seq - 1]
            if e.type == "assistant/message"
        )
        or not any(t.name == SKILL_TOOL_NAME for t in composition.tools)
        or call.composition_revision != composition.revision
        or request["catalog_digest"] != composition.skill_catalog_digest
        or not (
            search_grant
            or any(
                b["kind"] == "skill"
                and b["id"] == request["skill_id"]
                and b["version"] == request["version"]
                for b in context["blocks"]
            )
        )
        or context["policy"]["config"]["skills"] != policy.to_dict()
    ):
        raise ValueError(
            "skill-request-not-disclosed: use a currently visible reference or copy the exact "
            "read_action from this Step's search hit. A summary search hit does not grant "
            "directory or another chapter. Use search_skill with subject keywords to locate "
            "the needed section or resource chunk."
        )
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
        "format": RECEIPT_FORMAT,
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "source_step_id": context.step_id,
        "tool_call_id": context.tool_call_id,
        "context_ref": event_ref(source),
        "request": request,
        "policy_digest": policy.digest,
        "target_rule": TARGET_RULE,
    }
    return receipt, descriptor.navigation()


def eligible_requests(events, *, session_id, turn_id, step_id, policy):
    from traceh.session.reference_requests import eligible_requests as collect

    return collect(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        policy=policy,
        kind="skill",
        tool_name=SKILL_TOOL_NAME,
        parse_request=parse_request,
        source_for_request=source_for_request,
        matches=matches_block,
    )


def matches_block(request, block):
    return (
        block["kind"] == "skill"
        and request["skill_id"] == block["id"]
        and request["version"] == block["version"]
        and request["catalog_digest"] == block["provenance"]["catalog_digest"]
        and request["requested_tier"] == block["tier"]
        and all(
            request[key] == block["provenance"][key]
            for key in ("section_id", "resource_id", "chunk_id")
        )
    )
