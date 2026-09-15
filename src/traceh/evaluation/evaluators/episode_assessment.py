"""Read-only evidence checks. Exact answer matching is provisional, never a semantic judge."""

import json
import re

from traceh.api.history import HistoryReadPolicy
from traceh.api.json_types import fingerprint
from traceh.session.context_input import parse_context_input, render_context_message
from traceh.session.history import read_history
from traceh.session.tool_output import render_output_page, render_output_search, resolve_tool_output


def usage(events):
    attempts = [e.data for e in events if e.type == "model/attempt-end"]
    known = [
        a["usage"]
        for a in attempts
        if a.get("usage") is not None and a["usage"].get("quality") in {"exact", "estimated"}
    ]
    return {
        "attempts": len(attempts),
        "unknown_attempts": len(attempts) - len(known),
        "estimated_attempts": sum(u["quality"] == "estimated" for u in known),
        **{
            key: sum(u[key] for u in known) if len(known) == len(attempts) else None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        },
        "known_subtotal": {
            key: sum(u[key] for u in known)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        },
    }


def answer_matches(answer, expectation):
    value = expectation["value"]
    if expectation["kind"] != "value":
        return None
    if expectation["value_type"] == "number":
        return re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", answer) is not None
    return value in answer


def _context_evidence(context, events, case):
    """Sources were validated by SessionService + request replay, then qualify the asked fact."""
    family, expected = case["family"], case["expectation"]
    source, value = expected["source_text"], expected["value"]
    if not value or expected["kind"] != "value":
        return []
    found = []
    history = None
    if family == "history":
        history = read_history(
            events,
            session_id=context["session_id"],
            through_seq=context["observed_session_seq"],
            policy=HistoryReadPolicy.from_dict(context["policy"]["config"]["history"]),
        )
    for block in context["blocks"]:
        if (
            block["kind"] != family
            or block["tier"] == "directory"
            or (block["tier"] == "summary" and family != "memory")
        ):
            continue
        if block["tier"] == "search":
            for hit in json.loads(block["body"])["hits"]:
                ref = hit["reference"]
                if value not in hit["text"]:
                    continue
                if family == "history":
                    records = history.search_records()
                    valid = any(
                        record["reference"] == ref and source in record["text"]
                        for record in records
                    )
                elif family == "memory":
                    valid = ref["id"] == expected["reference_id"]
                else:
                    # Skill search indexes navigation, not hidden body. A matching code in
                    # metadata is not proof that the actual section/resource was disclosed.
                    valid = False
                if valid:
                    found.append({"reference": ref, "text": hit["text"], "tier": "search"})
        elif value in block["body"]:
            if family == "history":
                leaves = history.leaf_refs(block["id"])
                valid = any(
                    source in (events[ref["seq"] - 1].data.get("content") or "") for ref in leaves
                )
            else:
                valid = block["id"] == expected["reference_id"] and source in block["body"]
            if valid:
                found.append(
                    {
                        "reference": {
                            "id": block["id"],
                            "version": block["version"],
                            "source_refs": block["source_refs"],
                            "provenance": block["provenance"],
                        },
                        "text": block["body"],
                        "tier": block["tier"],
                    }
                )
    return found


def answer_dispatches(events, *, target_start, target_turn):
    """One definition of successful, owned requests before the observed final answer."""
    responses = [
        e
        for e in events
        if e.type == "assistant/message"
        and e.data["turn_id"] == target_turn
        and not e.data["tool_calls"]
    ]
    if not responses:
        return
    final = responses[-1]
    yield from successful_dispatches(
        events, target_start=target_start, target_turn=target_turn, before_seq=final.seq,
    )


def successful_dispatches(events, *, target_start, target_turn, before_seq=None):
    """Owned successful attempts, independently of whether the Turn produced a final answer."""
    by_seq = {e.seq: e for e in events}
    for attempt in events:
        data = attempt.data
        if (
            attempt.type != "model/attempt-end"
            or attempt.seq <= target_start
            or data["status"] != "succeeded"
            or data["turn_id"] != target_turn
            or not any(
                e.type == "assistant/message"
                and (before_seq is None or e.seq <= before_seq)
                and e.data["attempt_id"] == data["attempt_id"]
                for e in events
            )
        ):
            continue
        snapshot = by_seq[data["request_snapshot_seq"]]
        raw = snapshot.data
        if (
            snapshot.stream_id != attempt.stream_id
            or snapshot.seq >= attempt.seq
            or (before_seq is not None and snapshot.seq >= before_seq)
            or raw["turn_id"] != target_turn
            or raw["step_id"] != data["step_id"]
            or raw["dispatch_fingerprint"] != data["dispatch_fingerprint"]
            or fingerprint(raw["dispatch_request"]) != data["dispatch_fingerprint"]
        ):
            raise ValueError("evaluation-evidence-mismatch")
        yield attempt, snapshot


def dispatched_context(snapshot, by_seq):
    """Return only the context actually rendered in this frozen dispatch."""
    raw = snapshot.data
    context_event = by_seq[raw["context_input_seq"]]
    context = parse_context_input(context_event.data)
    messages = raw["dispatch_request"]["messages"]
    if (
        context_event.stream_id == snapshot.stream_id
        and context_event.seq < snapshot.seq
        and context_event.data["turn_id"] == raw["turn_id"]
        and messages
        and messages[-1] == render_context_message(context).to_dict()
    ):
        return context_event, context.to_dict()
    return None


def dispatched_evidence(events, effects, case, *, target_start, target_turn, max_chars):
    by_seq = {e.seq: e for e in events}
    found = []
    for attempt, snapshot in answer_dispatches(
        events, target_start=target_start, target_turn=target_turn
    ):
        data, raw = attempt.data, snapshot.data
        messages = raw["dispatch_request"]["messages"]
        receipts = []
        if case["family"] != "output":
            visible = dispatched_context(snapshot, by_seq)
            if visible is not None:
                context_event, context = visible
                receipts = [
                    {"context_seq": context_event.seq, **item}
                    for item in _context_evidence(context, events, case)
                ]
        else:
            for result in events:
                if (
                    result.type != "tool/result"
                    or not target_start < result.seq < snapshot.seq
                    or result.data["status"] != "succeeded"
                    or result.data["tool_name"] not in {"read_tool_output", "search_tool_output"}
                ):
                    continue
                content = result.data["content"]
                if not any(
                    m["role"] == "tool"
                    and m.get("tool_call_id") == result.data["tool_call_id"]
                    and m.get("content") == content
                    for m in messages
                ):
                    continue
                calls = [
                    e
                    for e in events
                    if e.type == "tool/call"
                    and e.data["tool_call_id"] == result.data["tool_call_id"]
                ]
                if len(calls) != 1:
                    raise ValueError("evaluation-evidence-mismatch")
                arguments = calls[0].data["arguments"]
                if not any(
                    e.type == "tool/result"
                    and e.seq <= target_start
                    and e.data["tool_name"] == "shell"
                    and e.data.get("output_ref", {}).get("effect_id") == arguments["effect_id"]
                    and e.data.get("output_ref", {}).get("digest") == arguments["digest"]
                    for e in events
                ):
                    raise ValueError("evaluation-evidence-mismatch")
                payload = resolve_tool_output(
                    events,
                    effects,
                    session_id=attempt.stream_id.removeprefix("session:"),
                    effect_id=arguments["effect_id"],
                    digest=arguments["digest"],
                )
                defaults = dict(part="content", offset=0, count=max_chars)
                renderer = render_output_page
                if result.data["tool_name"] == "search_tool_output":
                    defaults.update(count=10, context_lines=0, case_sensitive=True)
                    renderer = render_output_search
                rebuilt = renderer(payload, **{**defaults, **arguments}, max_chars=max_chars)
                if rebuilt != content:
                    raise ValueError("evaluation-evidence-mismatch")
                page = json.loads(content)
                texts = (
                    [page["text"]]
                    if "text" in page
                    else [
                        hit[key]
                        for hit in page["matches"]
                        for key in ("before_context", "matched_lines", "after_context")
                    ]
                )
                expected = case["expectation"]
                if (
                    expected["kind"] == "value"
                    and expected["source_text"] in payload["content"]
                    and any(expected["value"] in text for text in texts)
                ):
                    receipts.append(
                        {
                            "tool_result_seq": result.seq,
                            "reference": {
                                "effect_id": arguments["effect_id"],
                                "digest": arguments["digest"],
                            },
                            "text": content,
                        }
                    )
        for receipt in receipts:
            found.append(
                {
                    "session_id": attempt.stream_id.removeprefix("session:"),
                    "turn_id": target_turn,
                    "step_id": data["step_id"],
                    "attempt_id": data["attempt_id"],
                    "attempt_end_seq": attempt.seq,
                    "request_snapshot_seq": snapshot.seq,
                    "dispatch_fingerprint": data["dispatch_fingerprint"],
                    **receipt,
                }
            )
    return found
