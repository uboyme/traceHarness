"""Deterministic failure-mechanism detection over finished original evidence.

A detection is a count of events the Session itself recorded, with their exact
``stream@seq`` locations. It never reads a model's prose, never judges whether an
answer was right, and never uploads a transcript: the text sent to analysis is a
fixed template filled with counts and - for benchmark evaluation runs only - a
few bounded lines quoted from host-recorded tool failures (see ``_cause``). The marks
come from the same scan that feeds the evaluation report (``context_marks``), so a
report column and a detection can never disagree about what happened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from traceh.api.events import EventEnvelope
from traceh.api.json_types import fingerprint
from traceh.api.optimization import DevelopmentObservation, RuntimeObservation
from traceh.evaluation.evaluators.context_diagnostics import MARKS, context_marks

#: Most locations one observation cites. The analysis needs where to look, not
#: every occurrence; the count in the summary keeps the full number.
MAX_LOCATIONS = 8

#: Classes whose summary also names the host-recorded cause, the most distinct
#: causes one summary lists, and the longest line a cause may quote.
CAUSED_CLASSES = frozenset({"tool-failed", "tool-denied"})
MAX_CAUSES = 4
MAX_CAUSE_CHARS = 200

_LONG_ID = re.compile(r"\b[0-9a-f]{32,}\b")

_EXPLANATIONS = {
    "truncated-response": "responses ended because the output limit was reached",
    "empty-response": "replies had no text and no tool call",
    "rerun-after-fold": (
        "tool calls re-ran a tool with identical arguments after its earlier result had "
        "been folded, instead of reading the folded output back"
    ),
    "repeated-readback": "read-back calls reopened a page already read in this Session",
    "context-over-limit": "requests were refused before dispatch as over the input limit",
    "tool-failed": "tool results failed",
    "tool-denied": "tool calls were denied",
}


@dataclass(frozen=True, slots=True)
class Detection:
    """One mechanism seen in one Session, with the events that showed it."""

    failure_class: str
    count: int
    locations: tuple[str, ...]
    #: ``(cause, occurrences)`` for tool failures and denials, most frequent first.
    causes: tuple[tuple[str, int], ...] = ()

    @property
    def summary(self) -> str:
        causes = ""
        if self.causes:
            listed = "; ".join(f"{count}x {cause!r}" for cause, count in self.causes)
            causes = f" Host-recorded causes: {listed}."
        return (
            f"{self.count} {_EXPLANATIONS[self.failure_class]}.{causes} This is a structural "
            "signal from the original events, not proof of a wrong answer; some "
            "occurrences may be expected. Propose nothing unless a general text change "
            "addresses this mechanism."
        )


def _cause(result: EventEnvelope) -> str:
    """The tool and the host-written lines that say why its result failed.

    Only the result the host recorded is read - its first line and the first
    later line naming an error - never the model's reasoning. Without this the
    analysis saw "3 tool results failed" and proposed "diagnose and retry",
    while the result itself said the sandbox could not find an executable
    called ``PYTHONPATH=src``: the model had written shell syntax for a tool
    that runs argv. Long hex identifiers (receipts, digests) are masked so the
    same cause in two runs reads the same.
    """

    lines = [
        _LONG_ID.sub("<id>", line.strip())
        for line in str(result.data.get("content", "")).splitlines()
        if line.strip() and not line.strip().startswith("---")
    ]
    picked = lines[:1] + [line for line in lines[1:] if "error" in line.lower()][:1]
    text = f"{result.data.get('tool_name', '?')}: " + " | ".join(picked)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_CAUSE_CHARS]


def _causes(events: Sequence[EventEnvelope], seqs: Sequence[int]) -> tuple[tuple[str, int], ...]:
    by_seq = {event.seq: event for event in events}
    counts: dict[str, int] = {}
    for seq in seqs:
        event = by_seq.get(seq)
        if event is not None and event.type == "tool/result":
            cause = _cause(event)
            counts[cause] = counts.get(cause, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ranked[:MAX_CAUSES])


def detect(
    session_id: str,
    events: Sequence[EventEnvelope],
    *,
    stream_id: str,
    within: tuple[int, int] | None = None,
    quote_causes: bool = False,
) -> tuple[Detection, ...]:
    """Every mechanism the Session showed, optionally only inside ``within`` seqs.

    ``quote_causes`` is for benchmark evidence only. A chat or Product Session is
    a person's own work, and its tool results may hold their files or secrets, so
    those findings stay counts (ADR-0082); an evaluation run of a development
    case carries no such content, and there the cause is what makes a
    suggestion specific (ADR-0085).
    """

    marks = context_marks(session_id, events)
    found = []
    for name in MARKS:
        seqs = tuple(seq for seq in marks[name] if within is None or within[0] <= seq <= within[1])
        if seqs:
            found.append(
                Detection(
                    name,
                    len(seqs),
                    tuple(f"{stream_id}@{seq}" for seq in seqs[:MAX_LOCATIONS]),
                    _causes(events, seqs) if quote_causes and name in CAUSED_CLASSES else (),
                )
            )
    return tuple(found)


def turn_bounds(events: Sequence[EventEnvelope], turn_id: str) -> tuple[int, int] | None:
    """Seqs of one Turn's durable start and end, or ``None`` if it has not ended."""

    start = end = None
    for event in events:
        if event.data.get("turn_id") != turn_id:
            continue
        if event.type == "turn/start":
            start = event.seq
        elif event.type == "turn/end":
            end = event.seq
    return None if start is None or end is None else (start, end)


def runtime_observation(
    session_id: str,
    turn_id: str,
    events: Sequence[EventEnvelope],
    detection: Detection,
) -> RuntimeObservation:
    """A chat or Product finding, cut at the Turn end it was observed through."""

    ends = [e for e in events if e.type == "turn/end" and e.data.get("turn_id") == turn_id]
    cut = ends[-1].seq
    return RuntimeObservation(
        session_id,
        turn_id,
        cut,
        fingerprint(tuple(e for e in events if e.seq <= cut)),
        detection.failure_class,
        detection.summary,
        detection.locations,
    )


@dataclass(frozen=True, slots=True)
class EvaluationFinding:
    """One detection in one evaluation trial, bound to its development case."""

    source: str
    observation: DevelopmentObservation


def evaluation_findings(run_root: Path) -> tuple[EvaluationFinding, ...]:
    """Read one finished evaluation run through its original verifier, then detect.

    ``load_run`` re-checks the frozen condition, report and every evidence stream
    digest before any event is trusted; a run that fails it is refused, not skipped.
    """

    from traceh.evaluation.evidence import load_run

    run_root = Path(run_root).resolve()
    frozen, report, _ = load_run(run_root)
    findings = []
    for trial in report["trials"]:
        identity = trial["identity"]
        source = f"evaluation:{frozen['run_id']}/{identity['trial_id']}"
        for ref in trial["evidence"]:
            if not ref["stream_id"].startswith("session:"):
                continue
            with closing(
                sqlite3.connect((run_root / ref["file"]).as_uri() + "?mode=ro", uri=True)
            ) as database:
                rows = database.execute(
                    "SELECT envelope_json FROM events WHERE stream_id=? ORDER BY seq",
                    (ref["stream_id"],),
                ).fetchall()
            events = tuple(EventEnvelope.from_dict(json.loads(row[0])) for row in rows)
            session_id = ref["stream_id"].removeprefix("session:")
            for detection in detect(
                session_id, events, stream_id=ref["stream_id"], quote_causes=True
            ):
                findings.append(
                    EvaluationFinding(
                        source,
                        DevelopmentObservation(
                            identity["case_id"],
                            detection.failure_class,
                            detection.summary,
                            detection.locations,
                        ),
                    )
                )
    return tuple(findings)


__all__ = [
    "MAX_LOCATIONS",
    "Detection",
    "EvaluationFinding",
    "detect",
    "evaluation_findings",
    "runtime_observation",
    "turn_bounds",
]
