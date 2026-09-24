"""Retained Tool output: one Effect outcome, bounded Session presentation.

The original payload and its public reference commit together. No second Store,
file path, mutable cache, or re-execution is involved in reading an old output.
"""

from __future__ import annotations

import re

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json, fingerprint

#: Format 2 splits reference eligibility from first disclosure, so every result
#: with a complete outcome is addressable while only oversized ones are withheld
#: from the first view. A format 1 reference no longer validates and is refused
#: rather than reinterpreted, per the pre-1.0 protocol rule.
OUTPUT_FORMAT = 2
OUTPUT_READ_TOOL = "read_tool_output"
OUTPUT_LIST_TOOL = "list_tool_outputs"
OUTPUT_SEARCH_TOOL = "search_tool_output"

#: The read-back capability, named once. A host grants these to a role without
#: granting the rest of the default tools; they only ever read this Session's
#: own recorded outputs.
OUTPUT_TOOL_IDS = (OUTPUT_READ_TOOL, OUTPUT_LIST_TOOL, OUTPUT_SEARCH_TOOL)

#: How this result was first shown to the model. ``inline`` means the model
#: already saw the whole thing and the reference exists so it can be folded and
#: read back later; ``retained`` means the payload was withheld from the start.
INLINE = "inline"
RETAINED = "retained"

# Session control receipts drive the next Context, independently of model-facing
# output presentation. Their original domain owners still validate every grant.
_CONTROL_RECEIPTS = {
    "request_history_page": "history_receipt",
    "request_skill_reference": "skill_receipt",
    "request_workspace_memory": "memory_receipt",
    "search_history": "search_receipt",
    "search_skill": "search_receipt",
    "search_memory": "search_receipt",
}


def _control_data(tool_name, status, data):
    key = _CONTROL_RECEIPTS.get(tool_name)
    return {key: data[key]} if status == "succeeded" and key in data else {}


def prepare_tool_output(
    *,
    effect_id: str,
    tool_name: str,
    status: str,
    content: str,
    data: dict[str, JsonValue],
    evidence: tuple[str, ...],
    max_chars: int,
    reader_available: bool,
    searcher_available: bool = False,
) -> tuple[str, dict[str, JsonValue], dict[str, JsonValue]]:
    """Address every complete output; withhold only the oversized ones.

    Deciding addressability by this one result's size was the flaw: a long task
    accumulates hundreds of small results that each stay under the ceiling, so
    none of them ever became referenceable and none could later be folded. Size
    still decides what the model *sees first*; it no longer decides whether the
    result can be found again.
    """
    payload = {"content": content, "data": data, "evidence": list(evidence)}
    inline = len(content) <= max_chars and len(canonical_json(data)) <= max_chars
    reference: dict[str, JsonValue] = {
        "format": OUTPUT_FORMAT,
        "effect_id": effect_id,
        "digest": fingerprint(payload),
        "disclosure": INLINE if inline else RETAINED,
        "content_chars": len(content),
        "content_utf8_bytes": len(content.encode("utf-8")),
        "data_chars": len(canonical_json(data)),
    }
    if inline:
        # Shown in full exactly as before. The reference rides along so a later
        # fold has something precise to collapse to.
        return content, data, {"output_ref": reference, "retained_output": payload}
    display = {
        "notice": (
            "Tool output retained in this Session's Effect log. "
            + (
                "Content is not loaded here. "
                if reader_available or searcher_available
                else "Preview is incomplete. "
            )
            + "Do not rerun the original tool to recover this historical result. "
            + (
                "Use read_tool_output for original content or structured data."
                if reader_available
                else "The host can inspect the original Effect outcome; "
                "no output reader is registered in this Tool composition."
            )
        ),
        "output_ref": reference,
    }
    if not reader_available and not searcher_available:
        display["preview"] = content[:max_chars]
    if searcher_available:
        display["search_tool"] = OUTPUT_SEARCH_TOOL
        display["notice"] += (
            " For a specific keyword or identifier, search_tool_output is the first lookup: "
            "pass this effect_id/digest and the keyword as query. Expand insufficient "
            "matches with their read_action. For browsing without a keyword, use "
            "read_tool_output with this reference."
        )
    elif reader_available:
        display["read_action"] = {
            "tool": OUTPUT_READ_TOOL,
            "arguments": {
                "effect_id": effect_id,
                "digest": reference["digest"],
                "part": "content",
                "offset": 0,
            },
        }
    presentation = canonical_json(display)
    return (
        presentation,
        _control_data(tool_name, status, data),
        {"output_ref": reference, "retained_output": payload},
    )


def output_reference(outcome: EventEnvelope) -> dict[str, JsonValue] | None:
    """Verify a host-owned retained variant; inline outcomes have no reference."""
    data = outcome.data
    if "retained_output" not in data and "output_ref" not in data:
        return None
    payload, reference = data.get("retained_output"), data.get("output_ref")
    if (
        outcome.type != "effect/outcome"
        or not isinstance(payload, dict)
        or set(payload) != {"content", "data", "evidence"}
        or type(payload["content"]) is not str
        or not isinstance(payload["data"], dict)
        or not isinstance(payload["evidence"], list)
        or any(type(item) is not str for item in payload["evidence"])
        or not isinstance(reference, dict)
    ):
        raise ValueError("tool-output-reference-invalid")
    disclosure = reference.get("disclosure")
    if disclosure not in (INLINE, RETAINED):
        raise ValueError("tool-output-reference-invalid")
    expected = {
        "format": OUTPUT_FORMAT,
        "effect_id": data.get("effect_id"),
        "digest": fingerprint(payload),
        "disclosure": disclosure,
        "content_chars": len(payload["content"]),
        "content_utf8_bytes": len(payload["content"].encode("utf-8")),
        "data_chars": len(canonical_json(payload["data"])),
    }
    if canonical_json(reference) != canonical_json(expected):
        raise ValueError("tool-output-reference-invalid")
    # What the result is allowed to still carry depends on which disclosure it
    # claims. An inline result must show the same bytes it stored, so a payload
    # cannot quietly diverge from the text the model read; a retained one must
    # have been reduced to its control receipts, so nothing large leaked past
    # the ceiling. Checking only one of these would let the other lie.
    if disclosure == RETAINED:
        if canonical_json(data.get("data", {})) != canonical_json(
            _control_data(data.get("tool_name"), data.get("status"), payload["data"])
        ):
            raise ValueError("tool-output-control-data-mismatch")
    elif canonical_json(data.get("data", {})) != canonical_json(
        payload["data"]
    ) or data.get("content", data.get("message")) != payload["content"]:
        raise ValueError("tool-output-inline-payload-mismatch")
    return reference


def resolve_tool_output(
    session_events: tuple[EventEnvelope, ...],
    effect_events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    effect_id: str,
    digest: str,
) -> dict[str, JsonValue]:
    """Bind an exact original result to this Session and its execution intent."""
    outcomes = [
        event
        for event in effect_events
        if event.type == "effect/outcome" and event.data.get("effect_id") == effect_id
    ]
    intents = [
        event
        for event in effect_events
        if event.type == "effect/intent" and event.data.get("effect_id") == effect_id
    ]
    results = [
        event
        for event in session_events
        if event.type == "tool/result" and event.data.get("effect_id") == effect_id
    ]
    if len(outcomes) != 1 or len(intents) != 1 or len(results) != 1:
        raise ValueError("tool-output-source-unavailable")
    outcome, intent, result = outcomes[0], intents[0], results[0]
    calls = [
        event
        for event in session_events
        if event.type == "tool/call"
        and event.data.get("tool_call_id") == result.data.get("tool_call_id")
    ]
    reference = output_reference(outcome)
    if (
        reference is None
        or reference["digest"] != digest
        or outcome.stream_id != f"effects:{session_id}"
        or intent.stream_id != outcome.stream_id
        or result.stream_id != f"session:{session_id}"
        or intent.data.get("session_id") != session_id
        or outcome.seq <= intent.seq
        or outcome.causation_id != intent.event_id
        or canonical_json(result.data.get("output_ref")) != canonical_json(reference)
        or result.data.get("step_id") != intent.data.get("step_id")
        or result.data.get("status") != outcome.data.get("status")
        or result.data.get("content") != outcome.data.get("content", outcome.data.get("message"))
        or canonical_json(result.data.get("data")) != canonical_json(outcome.data.get("data", {}))
        or len(calls) != 1
        or calls[0].seq >= result.seq
        or any(
            calls[0].data.get(key) != intent.data.get(key)
            for key in ("tool_call_id", "tool_name", "step_id", "turn_id", "arguments")
        )
        or any(
            result.data.get(key) != intent.data.get(key)
            or outcome.data.get(key) != intent.data.get(key)
            for key in ("tool_call_id", "tool_name")
        )
    ):
        raise ValueError("tool-output-source-binding-mismatch")
    return outcome.data["retained_output"]


def render_output_page(
    payload: dict[str, JsonValue],
    *,
    effect_id: str,
    digest: str,
    part: str,
    offset: int,
    count: int,
    max_chars: int,
) -> str:
    """Character offsets preserve Unicode; the complete serialized page is bounded."""
    if part not in {"content", "data"}:
        raise ValueError("tool-output-part-invalid")
    source = payload["content"] if part == "content" else canonical_json(payload["data"])
    if type(offset) is not int or not 0 <= offset <= len(source):
        raise ValueError("tool-output-offset-invalid")
    if type(count) is not int or count < 1:
        raise ValueError("tool-output-count-invalid")

    def render(length: int) -> str:
        end = offset + length
        return canonical_json(
            {
                "effect_id": effect_id,
                "digest": digest,
                "part": part,
                "offset_unit": "unicode-codepoints",
                "offset": offset,
                "end_offset": end,
                "body_status": "complete-source"
                if offset == 0 and end == len(source)
                else "source-excerpt",
                "total_chars": len(source),
                "next_offset": end if end < len(source) else None,
                "text": source[offset:end],
            }
        )

    lower, upper = 0, min(count, len(source) - offset)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if len(render(middle)) <= max_chars:
            lower = middle
        else:
            upper = middle - 1
    result = render(lower)
    if len(result) > max_chars or (not lower and offset < len(source)):
        raise ValueError("tool-output-page-budget-too-small")
    return result


def render_output_search(
    payload: dict[str, JsonValue],
    *,
    effect_id: str,
    digest: str,
    query: str,
    part: str,
    offset: int,
    count: int,
    context_lines: int,
    case_sensitive: bool,
    max_chars: int,
) -> str:
    """Literal grep over immutable text; offsets use the reader's exact source.

    Matches are non-overlapping. Pagination resumes after the last returned match,
    never after its context. A long line is excerpted without losing the match.
    """
    if part not in {"content", "data"}:
        raise ValueError("tool-output-part-invalid")
    source = payload["content"] if part == "content" else canonical_json(payload["data"])
    if not isinstance(query, str) or not query:
        raise ValueError("tool-output-query-invalid")
    if type(offset) is not int or not 0 <= offset <= len(source):
        raise ValueError("tool-output-offset-invalid")
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("tool-output-count-invalid")
    if type(context_lines) is not int or not 0 <= context_lines <= 20:
        raise ValueError("tool-output-context-invalid")
    if type(case_sensitive) is not bool:
        raise ValueError("tool-output-case-invalid")
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    found = pattern.finditer(source, offset)
    matches = []
    following = next(found, None)

    def render(next_offset):
        return canonical_json(
            {
                "effect_id": effect_id,
                "digest": digest,
                "part": part,
                "query": query,
                "match_mode": "literal-substring",
                "case_sensitive": case_sensitive,
                "offset_unit": "unicode-codepoints",
                "offset": offset,
                "total_chars": len(source),
                "matches": matches,
                "next_offset": next_offset,
            }
        )

    def fits(end):
        return max(len(render(end)), len(render(None))) <= max_chars

    while following is not None and len(matches) < count:
        hit = following
        start = source.rfind("\n", 0, hit.start()) + 1
        end = source.find("\n", hit.end())
        end = len(source) if end < 0 else end + 1
        line_start, line_end = start, end
        for _ in range(context_lines):
            start = source.rfind("\n", 0, max(0, start - 1)) + 1
            newline = source.find("\n", end)
            end = len(source) if newline < 0 else newline + 1

        def entry(radius, start=start, end=end, hit=hit, line_start=line_start, line_end=line_end):
            left, right = max(start, hit.start() - radius), min(end, hit.end() + radius)
            read_offset = max(left, line_start)
            return {
                "match_start": hit.start(),
                "match_end": hit.end(),
                "line_number": source.count("\n", 0, hit.start()) + 1,
                "text_offset": left,
                "before_context": source[left:read_offset],
                "matched_lines": source[read_offset : min(right, line_end)],
                "after_context": source[min(right, line_end) : right],
                "context_truncated": left != start or right != end,
                "read_action": {
                    "tool": OUTPUT_READ_TOOL,
                    "arguments": {
                        "effect_id": effect_id,
                        "digest": digest,
                        "part": part,
                        "offset": read_offset,
                        "count": max_chars,
                    },
                },
            }

        radius = max(hit.start() - start, end - hit.end())
        matches.append(entry(radius))
        # Reserve both continuation variants, including JSON null at EOF.
        if not fits(hit.end()):
            if len(matches) > 1:
                matches.pop()
                break
            lower, upper = 0, radius
            while lower < upper:
                middle = (lower + upper + 1) // 2
                matches[-1] = entry(middle)
                if fits(hit.end()):
                    lower = middle
                else:
                    upper = middle - 1
            matches[-1] = entry(lower)
            if not fits(hit.end()):
                raise ValueError("tool-output-page-budget-too-small")
        following = next(found, None)

    next_offset = matches[-1]["match_end"] if following is not None and matches else None
    result = render(next_offset)
    if len(result) > max_chars:
        raise ValueError("tool-output-page-budget-too-small")
    return result
