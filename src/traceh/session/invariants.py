"""Executable protocol invariants."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    name: str
    message: str
    seq: int | None = None
    details: dict[str, JsonValue] | None = None


class CoreInvariantChecker:
    def check(
        self,
        session_events: tuple[EventEnvelope, ...],
        effect_events: tuple[EventEnvelope, ...] = (),
    ) -> tuple[InvariantViolation, ...]:
        violations: list[InvariantViolation] = []
        expected_seq = 1
        open_turn: str | None = None
        open_step: str | None = None
        calls: dict[str, tuple[str, int]] = {}
        results: set[str] = set()
        closed_steps: set[str] = set()
        seen_seqs: set[int] = set()

        for event in session_events:
            if event.seq != expected_seq:
                violations.append(
                    InvariantViolation(
                        "event-sequence",
                        f"expected seq {expected_seq}, got {event.seq}",
                        event.seq,
                    )
                )
                expected_seq = event.seq
            expected_seq += 1

            if event.type == "surface/replace":
                raw_source_seqs = event.data.get("source_seqs", [])
                if not isinstance(raw_source_seqs, list) or not raw_source_seqs:
                    violations.append(
                        InvariantViolation(
                            "surface-replacement-sources",
                            "surface replacement must reference at least one source event",
                            event.seq,
                        )
                    )
                else:
                    try:
                        source_seqs = [int(item) for item in raw_source_seqs]
                    except (TypeError, ValueError):
                        source_seqs = []
                        violations.append(
                            InvariantViolation(
                                "surface-replacement-seq-format",
                                "surface replacement source sequences must be integers",
                                event.seq,
                            )
                        )
                    if len(source_seqs) != len(set(source_seqs)):
                        violations.append(
                            InvariantViolation(
                                "surface-replacement-unique",
                                "surface replacement contains duplicate source sequences",
                                event.seq,
                            )
                        )
                    missing = [seq for seq in source_seqs if seq not in seen_seqs]
                    if missing:
                        violations.append(
                            InvariantViolation(
                                "surface-replacement-earlier",
                                f"surface replacement references unknown or future seqs: {missing}",
                                event.seq,
                            )
                        )
                if not isinstance(event.data.get("replacement"), dict):
                    violations.append(
                        InvariantViolation(
                            "surface-replacement-payload",
                            "surface replacement must contain one message object",
                            event.seq,
                        )
                    )

            seen_seqs.add(event.seq)

            if event.type == "turn/start":
                turn_id = str(event.data.get("turn_id"))
                if open_turn is not None:
                    violations.append(
                        InvariantViolation(
                            "single-open-turn",
                            f"turn {turn_id} opened while {open_turn} is still open",
                            event.seq,
                        )
                    )
                open_turn = turn_id
            elif event.type == "step/start":
                step_id = str(event.data.get("step_id"))
                if open_turn is None:
                    violations.append(
                        InvariantViolation(
                            "step-inside-turn",
                            f"step {step_id} opened outside a turn",
                            event.seq,
                        )
                    )
                if open_step is not None:
                    violations.append(
                        InvariantViolation(
                            "single-open-step",
                            f"step {step_id} opened while {open_step} is still open",
                            event.seq,
                        )
                    )
                open_step = step_id
            elif event.type == "tool/call":
                call_id = str(event.data.get("tool_call_id"))
                calls[call_id] = (str(event.data.get("step_id")), event.seq)
            elif event.type == "tool/result":
                call_id = str(event.data.get("tool_call_id"))
                if call_id not in calls:
                    violations.append(
                        InvariantViolation(
                            "tool-result-has-call",
                            f"tool result {call_id} has no matching call",
                            event.seq,
                        )
                    )
                elif calls[call_id][0] != str(event.data.get("step_id")):
                    violations.append(
                        InvariantViolation(
                            "tool-result-same-step",
                            f"tool result {call_id} is not in its call step",
                            event.seq,
                        )
                    )
                if call_id in results:
                    violations.append(
                        InvariantViolation(
                            "single-tool-result",
                            f"tool call {call_id} has more than one result",
                            event.seq,
                        )
                    )
                results.add(call_id)
            elif event.type == "step/end":
                step_id = str(event.data.get("step_id"))
                if open_step != step_id:
                    violations.append(
                        InvariantViolation(
                            "step-close-matches",
                            f"closed step {step_id}, but open step is {open_step}",
                            event.seq,
                        )
                    )
                closed_steps.add(step_id)
                open_step = None
            elif event.type == "turn/end":
                turn_id = str(event.data.get("turn_id"))
                if open_step is not None:
                    violations.append(
                        InvariantViolation(
                            "turn-closes-after-step",
                            f"turn {turn_id} ended while step {open_step} is open",
                            event.seq,
                        )
                    )
                if open_turn != turn_id:
                    violations.append(
                        InvariantViolation(
                            "turn-close-matches",
                            f"closed turn {turn_id}, but open turn is {open_turn}",
                            event.seq,
                        )
                    )
                open_turn = None

        for call_id, (step_id, call_seq) in calls.items():
            if call_id not in results and step_id in closed_steps:
                violations.append(
                    InvariantViolation(
                        "tool-call-has-result",
                        f"tool call {call_id} in closed step {step_id} has no result",
                        call_seq,
                    )
                )

        intent_ids = {
            str(event.data.get("effect_id"))
            for event in effect_events
            if event.type == "effect/intent"
        }
        for event in effect_events:
            if event.type in {"effect/outcome", "effect/reconciled"}:
                effect_id = str(event.data.get("effect_id"))
                if effect_id not in intent_ids:
                    violations.append(
                        InvariantViolation(
                            "effect-outcome-has-intent",
                            f"effect outcome {effect_id} has no matching intent",
                            event.seq,
                        )
                    )

        completed_effect_ids = {
            str(event.data.get("effect_id"))
            for event in effect_events
            if event.type in {"effect/outcome", "effect/reconciled"}
        }
        if open_step is None:
            for event in effect_events:
                if event.type == "effect/intent":
                    effect_id = str(event.data.get("effect_id"))
                    if effect_id not in completed_effect_ids:
                        violations.append(
                            InvariantViolation(
                                "effect-intent-has-outcome",
                                f"effect intent {effect_id} has no outcome or reconciliation",
                                event.seq,
                            )
                        )

        return tuple(violations)
