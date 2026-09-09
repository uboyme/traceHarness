"""Bounded search derived from original readers and successful Step tool receipts.

No writer, index, pending queue or second disclosure lifetime lives here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from traceh.api.json_types import canonical_json, fingerprint
from traceh.session.history import read_history
from traceh.session.history_requests import _context_for_request, _step, event_ref


class ReferenceSearchError(ValueError):
    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


def _fail(code="reference-search-binding-mismatch"):
    raise ReferenceSearchError(code)


def parse_request(value, policy):
    if type(value) is not dict or not {"query"} <= set(value) <= {"query", "cursor", "limit"}:
        _fail("reference-search-request-invalid")
    query = value["query"]
    limit = value.get("limit", policy.max_blocks)
    cursor = value.get("cursor")
    if type(limit) is not int or not 0 < limit <= policy.max_blocks:
        raise ReferenceSearchError(
            "reference-search-request-invalid",
            f"limit must be an integer from 1 to {policy.max_blocks}. "
            "Correct or omit limit; this parameter error does not mean no evidence was found.",
        )
    if (
        type(query) is not str
        or not query.strip()
        or len(query.encode("utf-8")) > policy.max_query_bytes
    ):
        _fail("reference-search-request-invalid")
    if cursor is not None:
        if type(cursor) is not dict or set(cursor) != {
            "source_digest",
            "query_digest",
            "policy_digest",
            "offset",
        }:
            _fail("reference-search-cursor-invalid")
        if type(cursor["offset"]) is not int or cursor["offset"] <= 0:
            _fail("reference-search-cursor-invalid")
        for key in ("source_digest", "query_digest", "policy_digest"):
            if (
                not isinstance(cursor[key], str)
                or re.fullmatch("[0-9a-f]{64}", cursor[key]) is None
            ):
                _fail("reference-search-cursor-invalid")
    return {"query": query, "cursor": cursor, "limit": limit}


def policy_digest(policy):
    from traceh.session.context_input import CONTEXT_POLICY_VERSION

    return fingerprint({"version": CONTEXT_POLICY_VERSION, "config": policy.to_dict()})


def history_records(events, session_id, policy):
    if policy.history is None or policy.history_tier is None:
        _fail("reference-search-source-disabled")
    snapshot = read_history(
        events, session_id=session_id, through_seq=events[-1].seq, policy=policy.history
    )
    return snapshot.search_records()


def source_digest(records):
    return fingerprint(list(records))


@dataclass(frozen=True, slots=True)
class SearchSource:
    """Detached read result, not a store, scope resolver or mutable authority."""

    kind: str
    scope: dict
    records: tuple
    digest: str
    fields: tuple[str, ...]
    available: bool


def _call_source(events, *, session_id, turn_id, step_id, tool_call_id, kind, policy):
    calls = [
        e for e in events if e.type == "tool/call" and e.data.get("tool_call_id") == tool_call_id
    ]
    if len(calls) != 1:
        _fail()
    call = calls[0]
    step, starts = _step(events[: call.seq], turn_id, step_id)
    name = "search_" + kind
    if (
        call.stream_id != "session:" + session_id
        or call.data.get("turn_id") != turn_id
        or call.data.get("step_id") != step_id
        or call.data.get("tool_name") != name
        or any(e.type == "runtime/cancel-requested" for e in events[starts[0].seq :])
    ):
        _fail()
    request = parse_request(call.data.get("arguments"), policy)
    source_policy = getattr(policy, "skills" if kind == "skill" else kind)
    if source_policy is None:
        _fail("reference-search-source-disabled")
    if (
        sum(
            e.type == "tool/call" and e.data.get("tool_name") == name
            for e in events[step.seq : call.seq]
        )
        > source_policy.max_requests
    ):
        _fail("reference-search-request-limit")
    snapshots = [e for e in events[step.seq : call.seq - 1] if e.type == "request/snapshot"]
    if len(snapshots) != 1:
        _fail()
    snapshot = snapshots[0]
    context = _context_for_request(events, snapshot)
    if context["policy"]["config"] != policy.to_dict():
        _fail("reference-search-policy-mismatch")
    expected = {"id": tool_call_id, "name": name, "arguments": call.data["arguments"]}
    if not any(
        e.type == "assistant/message"
        and e.data.get("turn_id") == turn_id
        and e.data.get("step_id") == step_id
        and expected in e.data.get("tool_calls", [])
        for e in events[snapshot.seq : call.seq - 1]
    ):
        _fail("reference-search-no-model-call")
    composition = events[snapshot.data["source_seq"] - 1]
    if call.composition_revision != context["composition_revision"] or not any(
        t.get("name") == name for t in composition.data["tools"]
    ):
        _fail("reference-search-tool-not-exposed")
    return call, events[snapshot.data["context_input_seq"] - 1], context, request


def history_receipt(events, *, session_id, turn_id, step_id, tool_call_id, policy):
    call, context_event, context, request = _call_source(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        kind="history",
        policy=policy,
    )
    records = history_records(events[: call.seq], session_id, policy)
    digest = source_digest(records)
    cursor = request["cursor"]
    if cursor is not None:
        if (
            cursor["source_digest"] != digest
            or cursor["query_digest"] != fingerprint(request["query"])
            or cursor["policy_digest"] != policy_digest(policy)
            or cursor["offset"] >= len(records)
        ):
            _fail("reference-search-cursor-stale")
        # A plausible offset is not a grant: only a currently admitted page can
        # authorize this continuation, and that page is reconstructed below.
        disclosed = False
        for block in context["blocks"]:
            if block["kind"] == "history" and block["tier"] == "search":
                verify_history_block(block, context, events, policy)
                body = json.loads(block["body"])
                if body["query"] == request["query"] and body["next_cursor"] == cursor:
                    disclosed = True
        if not disclosed:
            _fail("reference-search-cursor-not-disclosed")
    return {
        "format": 1,
        "session_id": session_id,
        "turn_id": turn_id,
        "source_step_id": step_id,
        "tool_call_id": tool_call_id,
        "context_ref": event_ref(context_event),
        "request": request,
        "policy_digest": policy_digest(policy),
        "source_digest": digest,
        "target_rule": "next-step-only",
    }


def _block(receipt, result_event, source, policy, *, width, hit_limit):
    request = receipt["request"]
    records, digest = source.records, source.digest
    body = {
        "query": request["query"],
        "fields": list(source.fields),
        "status": "no-hit",
        "scanned": 0,
        "total": len(records),
        "hits": [],
        "next_cursor": None,
    }
    if digest != receipt["source_digest"] or not source.available:
        body.update(status="source-unavailable", total=0)
    else:
        offset = request["cursor"]["offset"] if request["cursor"] else 0
        matcher = re.compile(re.escape(request["query"]), re.IGNORECASE)
        for index in range(offset, len(records)):
            record = records[index]
            body["scanned"] += 1
            match = matcher.search(record["text"])
            if match is None:
                continue
            start = max(0, match.start() - width)
            end = min(len(record["text"]), match.end() + width)
            ref = record["reference"]
            body["hits"].append(
                {
                    "reference": ref,
                    "text": record["text"][start:end],
                    "text_offset": start,
                    "match_start": match.start(),
                    "match_end": match.end(),
                    "truncated": start != 0 or end != len(record["text"]),
                    "read_action": record["read_action"]
                    if source.kind != "history"
                    else (
                        {
                            "tool_name": "request_history_page",
                            "arguments": {
                                "block_id": ref["block_id"],
                                "cursor": ref["cursor"],
                                "requested_tier": "chunk",
                            },
                        }
                        if ref["readable"]
                        else None
                    ),
                }
            )
            if len(body["hits"]) >= hit_limit:
                if index + 1 < len(records):
                    body["next_cursor"] = {
                        "source_digest": digest,
                        "query_digest": fingerprint(request["query"]),
                        "policy_digest": policy_digest(policy),
                        "offset": index + 1,
                    }
                break
        if body["hits"]:
            body["status"] = "matches"
    return _wrap(body, receipt, result_event, source)


def _wrap(body, receipt, result_event, source):
    encoded = canonical_json(body)
    ref = event_ref(result_event)
    return {
        "kind": source.kind,
        "id": fingerprint({"request": receipt, "body": body}),
        "version": receipt["source_digest"],
        "tier": "search",
        "scope": source.scope,
        "source_refs": [ref],
        "body": encoded,
        "content_digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "content_bytes": len(encoded.encode("utf-8")),
        "provenance": {
            "request_ref": ref,
            "policy_digest": receipt["policy_digest"],
            "source_digest": receipt["source_digest"],
        },
    }


def history_page(receipt, event, records, policy):
    return search_page(
        receipt,
        event,
        SearchSource(
            "history",
            {"kind": "session", "session_id": receipt["session_id"], "project_binding": None},
            records,
            source_digest(records),
            ("message.content",),
            bool(records),
        ),
        policy,
    )


def search_page(receipt, event, source, policy):
    from traceh.session.context_input import ContextInputError, _budget

    # Derive snippet space from the existing item bound; never chop serialized
    # JSON or original tool messages. Halving terminates at exact match text.
    width = policy.item_bytes
    hit_limit = receipt["request"]["limit"]
    while True:
        block = _block(receipt, event, source, policy, width=width, hit_limit=hit_limit)
        try:
            _budget([block], policy)
            return block
        except ContextInputError as error:
            if error.code != "context-budget-exceeded":
                raise
        if width == 0:
            if hit_limit > 1:
                hit_limit -= 1
                continue
            body = json.loads(block["body"])
            body.update(status="resource-limit", scanned=0, hits=[], next_cursor=None)
            return _wrap(body, receipt, event, source)
        width //= 2


def history_candidates(events, *, session_id, turn_id, step_id, policy):
    if policy.history is None or policy.history_tier is None:
        return []
    results = eligible_results(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        policy=policy,
        kind="history",
    )
    if not results:
        return []
    records = history_records(events, session_id, policy)
    return [history_page(receipt, event, records, policy) for event, receipt in results]


def _receipt_builder(kind):
    if kind == "history":
        return history_receipt
    if kind == "memory":
        from traceh.session.memory_search import receipt

        return receipt
    if kind == "skill":
        from traceh.session.skill_search import receipt

        return receipt
    _fail("reference-search-source-disabled")


def eligible_results(events, *, session_id, turn_id, step_id, policy, kind):
    """The one immediate-Step lifetime, derived from existing events only."""
    step, starts = _step(events, turn_id, step_id)
    if len(starts) < 2 or any(
        e.type == "runtime/cancel-requested" for e in events[starts[0].seq :]
    ):
        return []
    previous = starts[-2]
    ends = [e for e in events[previous.seq : step.seq - 1] if e.type == "step/end"]
    if len(ends) != 1 or ends[0].data.get("reason") != "model_response":
        return []
    results = []
    for event in events[previous.seq : ends[0].seq - 1]:
        if event.type != "tool/result" or event.data.get("tool_name") != "search_" + kind:
            continue
        if (
            event.data.get("status") != "succeeded"
            or event.data.get("error_type") == "RecoveredAfterCrash"
        ):
            continue
        expected = _receipt_builder(kind)(
            events[: event.seq - 1],
            session_id=session_id,
            turn_id=turn_id,
            step_id=previous.data["step_id"],
            tool_call_id=event.data.get("tool_call_id"),
            policy=policy,
        )
        if event.data.get("step_id") != previous.data["step_id"]:
            _fail("reference-search-receipt-invalid")
        if canonical_json(event.data.get("data", {}).get("search_receipt")) != canonical_json(
            expected
        ):
            _fail("reference-search-receipt-invalid")
        results.append((event, expected))
    return results


def validate_search_events(events):
    """Prove receipts even if the next Step never committed a Context."""
    from traceh.session.context_input import _parse_policy

    for event in events:
        data = event.data.get("data")
        if (
            event.type != "tool/result"
            or not isinstance(data, dict)
            or "search_receipt" not in data
        ):
            continue
        if event.data.get("status") != "succeeded" or event.data.get("tool_name") not in {
            "search_history",
            "search_memory",
            "search_skill",
        }:
            _fail("reference-search-receipt-invalid")
        prefix = events[: event.seq - 1]
        starts = [e for e in prefix if e.type == "step/start"]
        if not starts:
            _fail()
        turn_id = starts[-1].data["turn_id"]
        step, _ = _step(prefix, turn_id, event.data.get("step_id"))
        contexts = [e for e in prefix[step.seq :] if e.type == "context/input"]
        if len(contexts) != 1:
            _fail()
        expected = _receipt_builder(event.data["tool_name"].removeprefix("search_"))(
            prefix,
            session_id=event.stream_id.removeprefix("session:"),
            turn_id=turn_id,
            step_id=event.data.get("step_id"),
            tool_call_id=event.data.get("tool_call_id"),
            policy=_parse_policy(contexts[0].data["policy"]),
        )
        if canonical_json(data["search_receipt"]) != canonical_json(expected):
            _fail("reference-search-receipt-invalid")


def verify_history_block(block, context, events, policy):
    candidates = history_candidates(
        events[: context["observed_session_seq"]],
        session_id=context["session_id"],
        turn_id=context["turn_id"],
        step_id=context["step_id"],
        policy=policy,
    )
    if canonical_json(block) not in {canonical_json(item) for item in candidates}:
        _fail("reference-search-source-mismatch")


def history_discloses(events, request_event, request, context):
    from traceh.session.context_input import _parse_policy

    # Typed user requests may reuse older original-page navigation under their
    # existing contract, but search grants expire with their own Step.
    active_steps = [e for e in events if e.type == "step/start"]
    if not active_steps or active_steps[-1].data.get("step_id") != context["step_id"]:
        return False
    policy = _parse_policy(context["policy"])
    for block in context["blocks"]:
        if block["kind"] != "history" or block["tier"] != "search":
            continue
        verify_history_block(block, context, events, policy)
        for hit in json.loads(block["body"])["hits"]:
            action = hit["read_action"]
            if action and action["arguments"] == request.to_dict():
                return True
    return False


def validate_block(block, session_id, policy):
    """Strict wire shape; the Context source validator proves original contents."""
    from traceh.api.history import HistoryCursor
    from traceh.session.context_input import (
        _body_digest,
        _digest,
        _integer,
        _object,
        _reference,
        _session_scope,
        _text,
    )

    kind = block["kind"]
    if (
        kind not in {"history", "memory", "skill"}
        or getattr(policy, "skills" if kind == "skill" else kind) is None
    ):
        _fail("reference-search-source-disabled")
    if kind == "history" and policy.history_tier is None:
        _fail("reference-search-source-disabled")
    if kind == "memory" and block["scope"] != _session_scope(session_id):
        from traceh.memory.context import validate_scope

        validate_scope(block["scope"], session_id)
    elif block["scope"] != _session_scope(session_id):
        _fail()
    _digest(block["id"])
    _digest(block["version"])
    body_text = _text(block["body"])
    if (
        type(block["content_bytes"]) is not int
        or block["content_bytes"] != len(body_text.encode("utf-8"))
        or block["content_digest"] != _body_digest(body_text)
    ):
        _fail("reference-search-content-mismatch")
    provenance = _object(block["provenance"], {"request_ref", "policy_digest", "source_digest"})
    ref = _reference(provenance["request_ref"], session_id)
    _digest(provenance["source_digest"])
    if (
        ref["type"] != "tool/result"
        or block["source_refs"] != [ref]
        or provenance["policy_digest"] != policy_digest(policy)
        or block["version"] != provenance["source_digest"]
    ):
        _fail()
    body = _object(
        json.loads(body_text),
        {"query", "fields", "status", "scanned", "total", "hits", "next_cursor"},
    )
    parse_request({"query": body["query"], "cursor": body["next_cursor"]}, policy)
    fields = ["message.content"] if kind == "history" else ["memory_id", "fact_slot", "body"]
    if kind == "skill":
        from traceh.session.skill_search import FIELDS

        fields = list(FIELDS)
    if canonical_json(body) != body_text or body["fields"] != fields:
        _fail()
    if body["status"] not in {"matches", "no-hit", "source-unavailable", "resource-limit"}:
        _fail()
    _integer(body["scanned"])
    _integer(body["total"])
    if (
        body["scanned"] > body["total"]
        or type(body["hits"]) is not list
        or len(body["hits"]) > policy.max_blocks
    ):
        _fail()
    if (body["status"] == "matches") != bool(body["hits"]):
        _fail()
    if body["status"] in {"source-unavailable", "resource-limit"} and (
        body["hits"] or body["next_cursor"] is not None or body["scanned"] != 0
    ):
        _fail()
    for hit in body["hits"]:
        _object(
            hit,
            {
                "reference",
                "text",
                "text_offset",
                "match_start",
                "match_end",
                "truncated",
                "read_action",
            },
        )
        if kind == "skill":
            from traceh.session.skill_requests import parse_request as parse_skill_request

            reference = parse_skill_request(hit["reference"])
            _digest(reference["catalog_digest"])
            if hit["read_action"] != {
                "tool_name": "request_skill_reference",
                "arguments": reference,
            }:
                _fail()
            _validate_hit_text(hit)
            continue
        if kind == "memory":
            from traceh.session.context_input import _validate_block

            reference = hit["reference"]
            if (
                type(reference) is not dict
                or reference.get("kind") != "memory"
                or reference.get("tier") != "directory"
            ):
                _fail()
            _validate_block(reference, session_id, policy)
            expected = {
                "tool_name": "request_workspace_memory",
                "arguments": {
                    "memory_id": reference["id"],
                    "version": reference["version"],
                    "requested_tier": "section",
                },
            }
            if hit["read_action"] != expected or reference["scope"] != block["scope"]:
                _fail()
            _validate_hit_text(hit)
            continue
        reference = _object(
            hit["reference"], {"block_id", "version", "leaf_ref", "cursor", "readable"}
        )
        _digest(reference["block_id"])
        _digest(reference["version"])
        _reference(reference["leaf_ref"], session_id)
        cursor = HistoryCursor.from_dict(reference["cursor"])
        if (
            cursor.block_id != reference["block_id"]
            or cursor.policy_digest != policy.history.digest
        ):
            _fail()
        if type(reference["readable"]) is not bool or type(hit["truncated"]) is not bool:
            _fail()
        _text(hit["text"])
        for key in ("text_offset", "match_start", "match_end"):
            _integer(hit[key])
        if (
            not hit["text_offset"]
            <= hit["match_start"]
            < hit["match_end"]
            <= hit["text_offset"] + len(hit["text"])
        ):
            _fail()
        expected = (
            {
                "tool_name": "request_history_page",
                "arguments": {
                    "block_id": reference["block_id"],
                    "cursor": cursor.to_dict(),
                    "requested_tier": "chunk",
                },
            }
            if reference["readable"]
            else None
        )
        if hit["read_action"] != expected:
            _fail()
    return block


def _validate_hit_text(hit):
    from traceh.session.context_input import _integer, _text

    _text(hit["text"])
    if type(hit["truncated"]) is not bool:
        _fail()
    for key in ("text_offset", "match_start", "match_end"):
        _integer(hit[key])
    if (
        not hit["text_offset"]
        <= hit["match_start"]
        < hit["match_end"]
        <= hit["text_offset"] + len(hit["text"])
    ):
        _fail()
