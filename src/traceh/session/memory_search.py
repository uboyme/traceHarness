"""Search current approved Memory through the existing context reader and projection."""

import json

from traceh.api.json_types import canonical_json, fingerprint
from traceh.memory.context import make_block, prepare_corpus
from traceh.session.history_requests import event_ref
from traceh.session.reference_search import (
    SearchSource,
    _call_source,
    _fail,
    eligible_results,
    policy_digest,
    search_page,
)

FIELDS = ("memory_id", "fact_slot", "body")


def binding_digest(scope, heads, source_policy):
    return fingerprint({"scope": scope, "source_heads": heads, "memory_source": source_policy})


def frozen_binding(context):
    return binding_digest(context["scope"], context["source_heads"], context["memory_source"])


def source_view(source, session_id, policy):
    if source is None:
        scope = {"kind": "session", "session_id": session_id, "project_binding": None}
        return SearchSource("memory", scope, (), binding_digest(scope, [], None), FIELDS, False)
    # Reuse the original corpus owner's resource checks before exposing records.
    prepare_corpus(source, policy.memory)
    records = tuple(
        {
            "reference": make_block(fact, "directory", source),
            "text": "\n".join((fact.memory_id, fact.fact_slot, fact.body)),
            "read_action": {
                "tool_name": "request_workspace_memory",
                "arguments": {
                    "memory_id": fact.memory_id,
                    "version": fact.proposal.data["proposal_digest"],
                    "requested_tier": "section",
                },
            },
        }
        for fact in sorted(source.view.active, key=lambda fact: fact.memory_id)
    )
    if len(canonical_json(list(records)).encode("utf-8")) > policy.memory.max_corpus_bytes:
        _fail("reference-search-source-resource-limit")
    return SearchSource(
        "memory",
        source.scope,
        records,
        binding_digest(source.scope, source.heads, source.policy),
        FIELDS,
        True,
    )


def receipt(events, *, session_id, turn_id, step_id, tool_call_id, policy):
    _, event, context, request = _call_source(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        kind="memory",
        policy=policy,
    )
    digest = frozen_binding(context)
    cursor = request["cursor"]
    if cursor is not None:
        if (
            cursor["source_digest"] != digest
            or cursor["query_digest"] != fingerprint(request["query"])
            or cursor["policy_digest"] != policy_digest(policy)
        ):
            _fail("reference-search-cursor-stale")
        found = False
        for block in context["blocks"]:
            if block["kind"] == "memory" and block["tier"] == "search":
                validate_request_binding(block, context, events, policy)
                body = json.loads(block["body"])
                if body["query"] == request["query"] and body["next_cursor"] == cursor:
                    found = True
        if not found:
            _fail("reference-search-cursor-not-disclosed")
    return {
        "format": 1,
        "session_id": session_id,
        "turn_id": turn_id,
        "source_step_id": step_id,
        "tool_call_id": tool_call_id,
        "context_ref": event_ref(event),
        "request": request,
        "policy_digest": policy_digest(policy),
        "source_digest": digest,
        "target_rule": "next-step-only",
    }


def candidates(events, *, session_id, turn_id, step_id, policy, source):
    results = eligible_results(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        policy=policy,
        kind="memory",
    )
    if not results:
        return []
    view = source_view(source, session_id, policy)
    return [search_page(request, event, view, policy) for event, request in results]


def validate_request_binding(block, context, events, policy):
    body = json.loads(block["body"])
    for event, request in eligible_results(
        events[: context["observed_session_seq"]],
        session_id=context["session_id"],
        turn_id=context["turn_id"],
        step_id=context["step_id"],
        policy=policy,
        kind="memory",
    ):
        if block["provenance"]["request_ref"] == event_ref(event):
            if (
                block["id"] != fingerprint({"request": request, "body": body})
                or body["query"] != request["request"]["query"]
                or block["version"] != request["source_digest"]
            ):
                _fail("reference-search-receipt-invalid")
            return
    _fail("reference-search-expired")


def disclosed(events, context, request, policy):
    blocks = []
    for block in context["blocks"]:
        if block["kind"] != "memory" or block["tier"] != "search":
            continue
        validate_request_binding(block, context, events, policy)
        for hit in json.loads(block["body"])["hits"]:
            if hit["read_action"] and hit["read_action"]["arguments"] == request:
                blocks.append(hit["reference"])
    # The same approved object can occur in multiple admitted searches.
    return list({canonical_json(block): block for block in blocks}.values())


def verify_pages(data, events, source, policy):
    expected = candidates(
        events[: data["observed_session_seq"]],
        session_id=data["session_id"],
        turn_id=data["turn_id"],
        step_id=data["step_id"],
        policy=policy,
        source=source,
    )
    allowed = {canonical_json(block) for block in expected}
    for block in data["blocks"]:
        if (
            block["kind"] == "memory"
            and block["tier"] == "search"
            and canonical_json(block) not in allowed
        ):
            _fail("reference-search-source-mismatch")
