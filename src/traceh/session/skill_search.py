"""Search selected immutable Skill navigation; original leased readers own body reads."""

import json

from traceh.api.json_types import canonical_json, fingerprint
from traceh.session.history_requests import event_ref
from traceh.session.reference_search import (
    SearchSource,
    _call_source,
    _fail,
    eligible_results,
    policy_digest,
    search_page,
)
from traceh.session.skill_selection import head_ref

FIELDS = ("id", "title", "summary", "tags")


def binding_digest(session_id, catalog_digest, selection_head):
    return fingerprint(
        {
            "session_id": session_id,
            "catalog_digest": catalog_digest,
            "selection_head": selection_head,
        }
    )


def frozen_binding(context):
    return binding_digest(
        context["session_id"], context["skill_catalog_digest"], context["selection_head"]
    )


def records(descriptors, catalog_digest):
    result = []
    for descriptor in sorted(descriptors, key=lambda item: item.skill_id):
        base = {
            "skill_id": descriptor.skill_id,
            "version": descriptor.version,
            "catalog_digest": catalog_digest,
            "requested_tier": "summary",
            "section_id": None,
            "resource_id": None,
            "chunk_id": None,
        }

        def append(request, values):
            result.append(
                {
                    "reference": request,
                    "text": "\n".join(values),
                    "read_action": {"tool_name": "request_skill_reference", "arguments": request},
                }
            )

        append(base, (descriptor.skill_id, descriptor.title, descriptor.summary, *descriptor.tags))
        for section in descriptor.sections:
            append(
                {**base, "requested_tier": "section", "section_id": section.section_id},
                (descriptor.skill_id, section.section_id, section.title, section.summary),
            )
        for resource in descriptor.resources:
            parent = (descriptor.skill_id, resource.resource_id, resource.title, resource.summary)
            if not resource.chunks:
                append({**base, "requested_tier": "directory"}, parent)
            for chunk in resource.chunks:
                append(
                    {
                        **base,
                        "requested_tier": "chunk",
                        "resource_id": resource.resource_id,
                        "chunk_id": chunk.chunk_id,
                    },
                    (*parent, chunk.chunk_id, chunk.title, chunk.summary),
                )
    return tuple(result)


def source_view(composition, selections, session_id, policy):
    from traceh.session.skill_retrieval import prepare_corpus

    _, _, _, descriptors, reason = prepare_corpus(
        composition, selections, session_id, policy.skills
    )
    found = records(descriptors, composition.skill_catalog_digest)
    if (
        len(found) > policy.skills.max_corpus_items
        or len(canonical_json(list(found)).encode("utf-8")) > policy.skills.max_corpus_bytes
    ):
        _fail("reference-search-source-resource-limit")
    return SearchSource(
        "skill",
        {"kind": "session", "session_id": session_id, "project_binding": None},
        found,
        binding_digest(
            session_id, composition.skill_catalog_digest, head_ref(session_id, selections)
        ),
        FIELDS,
        reason is None and bool(composition.skill_catalog),
    )


def receipt(events, *, session_id, turn_id, step_id, tool_call_id, policy):
    _, event, context, request = _call_source(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        kind="skill",
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
            if block["kind"] == "skill" and block["tier"] == "search":
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


def candidates(events, *, session_id, turn_id, step_id, policy, composition, selections):
    results = eligible_results(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        policy=policy,
        kind="skill",
    )
    if not results:
        return []
    view = source_view(composition, selections, session_id, policy)
    return [search_page(request, event, view, policy) for event, request in results]


def validate_request_binding(block, context, events, policy):
    body = json.loads(block["body"])
    for event, request in eligible_results(
        events[: context["observed_session_seq"]],
        session_id=context["session_id"],
        turn_id=context["turn_id"],
        step_id=context["step_id"],
        policy=policy,
        kind="skill",
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
        if block["kind"] != "skill" or block["tier"] != "search":
            continue
        validate_request_binding(block, context, events, policy)
        for hit in json.loads(block["body"])["hits"]:
            if hit["read_action"] and hit["read_action"]["arguments"] == request:
                blocks.append(hit["reference"])
    # The same approved object can occur in multiple admitted searches.
    return list({canonical_json(block): block for block in blocks}.values())


def verify_pages(data, events, composition, selections, policy):
    expected = candidates(
        events[: data["observed_session_seq"]],
        session_id=data["session_id"],
        turn_id=data["turn_id"],
        step_id=data["step_id"],
        policy=policy,
        composition=composition,
        selections=selections,
    )
    allowed = {canonical_json(block) for block in expected}
    for block in data["blocks"]:
        if (
            block["kind"] == "skill"
            and block["tier"] == "search"
            and canonical_json(block) not in allowed
        ):
            _fail("reference-search-source-mismatch")


def verify_catalog_hits(data, composition):
    """Replay verifies snippet bytes against the frozen catalog, without a live Lease."""
    pages = [
        block for block in data["blocks"] if block["kind"] == "skill" and block["tier"] == "search"
    ]
    if not pages:
        return
    original = records(composition.skill_catalog, composition.skill_catalog_digest)
    for block in pages:
        for hit in json.loads(block["body"])["hits"]:
            offset = hit["text_offset"]
            if not any(
                record["reference"] == hit["reference"]
                and record["text"][offset : offset + len(hit["text"])] == hit["text"]
                for record in original
            ):
                _fail("reference-search-source-mismatch")
