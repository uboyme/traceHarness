"""What one Session's own events say about its context pressure and folding.

Everything here is derived from the durable Session stream and nothing else;
it is recomputed on every read and never written back. The numbers exist so a
comparison can show *where* two arms differed - peak request size, how often
folded evidence was reopened or re-fetched - but they never decide a winner.
That stays with the comparison contract that already owns it.

Two input sizes are reported because they are different measurements:

=========================  ==================================================
``input_*``                what the provider reported per dispatched attempt
                           (``model/attempt-end`` usage). Any attempt without a
                           trusted usage makes these unavailable, never zero.
``metered_*``              the host's pre-dispatch count of the frozen request
                           (``request/token-measurement``). Present only when
                           the role had a token policy; a request refused as
                           over the limit is counted here and never dispatched.
=========================  ==================================================

A *rerun after fold* is a later ``tool/call`` whose tool and canonical
arguments equal those of a call whose result had already been folded - the
exact behaviour the fold placeholder asks the model not to do. Read-back tools
are excluded: reopening folded evidence is the intended path, and repeated
opening of the same page is counted separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.evaluation.errors import BenchmarkEvidenceError
from traceh.llm.token_meter import TOKEN_MEASUREMENT
from traceh.session.surface_replacement import (
    SURFACE_COMPACTION_FAILED,
    SURFACE_REPLACE,
    SurfaceToolFold,
    fold_source_call,
    parse_surface_replacement,
)
from traceh.session.tool_output import OUTPUT_READ_TOOL, OUTPUT_TOOL_IDS

_TRUSTED_QUALITIES = frozenset({"exact", "estimated"})


@dataclass(frozen=True, slots=True)
class ContextWork:
    """Context pressure and folding behaviour of one Session."""

    requests: int
    unknown_usage_requests: int
    input_peak: int | None
    input_mean: float | None
    input_last: int | None
    metered_requests: int
    metered_peak: int | None
    trigger_tokens: int | None
    input_limit: int | None
    over_limit_requests: int
    tool_folds: int
    folded_source_utf8_bytes: int
    summaries: int
    compaction_failures: int
    readback_calls: int
    readback_repeats: int
    reruns_after_fold: int
    completions: dict[str, int]
    empty_responses: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "requests": self.requests,
            "unknown_usage_requests": self.unknown_usage_requests,
            "input_peak": self.input_peak,
            "input_mean": None if self.input_mean is None else round(self.input_mean, 1),
            "input_last": self.input_last,
            "metered_requests": self.metered_requests,
            "metered_peak": self.metered_peak,
            "trigger_tokens": self.trigger_tokens,
            "input_limit": self.input_limit,
            "over_limit_requests": self.over_limit_requests,
            "tool_folds": self.tool_folds,
            "folded_source_utf8_bytes": self.folded_source_utf8_bytes,
            "summaries": self.summaries,
            "compaction_failures": self.compaction_failures,
            "readback_calls": self.readback_calls,
            "readback_repeats": self.readback_repeats,
            "reruns_after_fold": self.reruns_after_fold,
            "completions": dict(sorted(self.completions.items())),
            "empty_responses": self.empty_responses,
        }


#: Evidence marks a detector may cite. Each maps to the ``seq`` of every event that
#: showed it, so a count in the report and a citation in a detection are the same fact.
MARKS = (
    "truncated-response",
    "empty-response",
    "rerun-after-fold",
    "repeated-readback",
    "context-over-limit",
    "tool-failed",
    "tool-denied",
)


def context_work(session_id: str, events: Sequence[EventEnvelope]) -> ContextWork:
    """Derive one Session's context diagnostics from its already-validated events."""

    return _scan(session_id, events)[0]


def context_marks(session_id: str, events: Sequence[EventEnvelope]) -> dict[str, tuple[int, ...]]:
    """The ``seq`` of every event carrying one of :data:`MARKS`, from the same scan."""

    return _scan(session_id, events)[1]


def _scan(
    session_id: str, events: Sequence[EventEnvelope]
) -> tuple[ContextWork, dict[str, tuple[int, ...]]]:
    by_seq = {event.seq: event for event in events}
    marks: dict[str, list[int]] = {name: [] for name in MARKS}
    inputs: list[int] = []
    unknown_usage = 0
    requests = 0
    metered: list[int] = []
    trigger_tokens: int | None = None
    input_limit: int | None = None
    tool_folds = 0
    folded_bytes = 0
    summaries = 0
    compaction_failures = 0
    readback_calls = 0
    readback_seen: set[str] = set()
    folded_at: dict[str, int] = {}
    completions: dict[str, int] = {}
    for event in events:
        data = event.data if type(event.data) is dict else {}
        if event.type == "model/attempt-end":
            requests += 1
            usage = data.get("usage")
            raw = usage.get("input_tokens") if type(usage) is dict else None
            quality = usage.get("quality") if type(usage) is dict else None
            if type(raw) is int and raw >= 0 and quality in _TRUSTED_QUALITIES:
                inputs.append(raw)
            else:
                unknown_usage += 1
            completion = data.get("completion")
            if type(completion) is str:
                completions[completion] = completions.get(completion, 0) + 1
                if completion == "length":
                    marks["truncated-response"].append(event.seq)
        elif event.type == TOKEN_MEASUREMENT:
            measurement = data.get("measurement")
            if type(measurement) is not dict or type(measurement.get("input_tokens")) is not int:
                raise BenchmarkEvidenceError("benchmark-context-evidence-invalid", session_id)
            metered.append(measurement["input_tokens"])
            trigger_tokens = measurement.get("trigger_tokens")
            input_limit = measurement.get("input_limit")
            if measurement.get("over_limit") is True:
                marks["context-over-limit"].append(event.seq)
        elif event.type == SURFACE_REPLACE:
            try:
                replacement = parse_surface_replacement(event)
                if isinstance(replacement, SurfaceToolFold):
                    call = fold_source_call(tuple(events), by_seq[replacement.source_seqs[0]])
                    key = _call_key(call)
                    if key is not None:
                        folded_at.setdefault(key, event.seq)
            except (KeyError, ValueError):
                raise BenchmarkEvidenceError(
                    "benchmark-context-evidence-invalid", session_id
                ) from None
            if isinstance(replacement, SurfaceToolFold):
                tool_folds += 1
                folded_bytes += replacement.source_utf8_bytes
            else:
                summaries += 1
        elif event.type == SURFACE_COMPACTION_FAILED:
            compaction_failures += 1
        elif event.type == "tool/call":
            if data.get("tool_name") == OUTPUT_READ_TOOL:
                readback_calls += 1
                page = canonical_json(data.get("arguments"))
                if page in readback_seen:
                    marks["repeated-readback"].append(event.seq)
                readback_seen.add(page)
            key = _call_key(event)
            if key is not None and folded_at.get(key, event.seq) < event.seq:
                marks["rerun-after-fold"].append(event.seq)
        elif event.type == "tool/result":
            status = data.get("status")
            if status == "failed":
                marks["tool-failed"].append(event.seq)
            elif status == "denied":
                marks["tool-denied"].append(event.seq)
        elif event.type == "assistant/message":
            content = data.get("content")
            if not data.get("tool_calls") and (content is None or not str(content).strip()):
                marks["empty-response"].append(event.seq)
    known = inputs and not unknown_usage
    work = ContextWork(
        requests=requests,
        unknown_usage_requests=unknown_usage,
        input_peak=max(inputs) if known else None,
        input_mean=sum(inputs) / len(inputs) if known else None,
        input_last=inputs[-1] if known else None,
        metered_requests=len(metered),
        metered_peak=max(metered) if metered else None,
        trigger_tokens=trigger_tokens,
        input_limit=input_limit,
        over_limit_requests=len(marks["context-over-limit"]),
        tool_folds=tool_folds,
        folded_source_utf8_bytes=folded_bytes,
        summaries=summaries,
        compaction_failures=compaction_failures,
        readback_calls=readback_calls,
        readback_repeats=len(marks["repeated-readback"]),
        reruns_after_fold=len(marks["rerun-after-fold"]),
        completions=completions,
        empty_responses=len(marks["empty-response"]),
    )
    return work, {name: tuple(seqs) for name, seqs in marks.items()}


def _call_key(call: EventEnvelope) -> str | None:
    """Tool identity plus canonical arguments; read-back tools never qualify."""

    name = call.data.get("tool_name") if type(call.data) is dict else None
    if type(name) is not str or name in OUTPUT_TOOL_IDS:
        return None
    return canonical_json({"tool": name, "arguments": call.data.get("arguments")})


__all__ = ["MARKS", "ContextWork", "context_marks", "context_work"]
