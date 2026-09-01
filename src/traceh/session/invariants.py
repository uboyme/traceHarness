"""Executable protocol invariants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple
from uuid import UUID

from traceh.api.events import EventEnvelope, attempt_identity
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelAttemptIdentity,
    ModelRequest,
    dispatch_request_matches_composed,
    model_attempt_reservation_id,
)
from traceh.session.plugin_identity import validate_plugin_identity_events
from traceh.session.product_context import (
    PRODUCT_CONTEXT_SNAPSHOT,
    parse_product_context_snapshot,
)


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    name: str
    message: str
    seq: int | None = None
    details: dict[str, JsonValue] | None = None


class _AttemptStart(NamedTuple):
    turn_id: str
    step_id: str
    seq: int
    event_id: UUID
    ordinal: int | None
    request_snapshot_seq: int | None
    dispatch_fingerprint: str | None
    reservation_id: str | None


_ATTEMPT_START_KEYS = frozenset(
    {
        "turn_id",
        "step_id",
        "attempt_id",
        "ordinal",
        "request_snapshot_seq",
        "dispatch_fingerprint",
        "reservation_id",
        "provider",
        "model",
        "retry_wait_milliseconds",
        "retry_failure_code",
        "retry_failure_category",
    }
)


class CoreInvariantChecker:
    def check(
        self,
        session_events: tuple[EventEnvelope, ...],
        effect_events: tuple[EventEnvelope, ...] = (),
    ) -> tuple[InvariantViolation, ...]:
        violations: list[InvariantViolation] = []
        for issue in validate_plugin_identity_events(session_events):
            violations.append(
                InvariantViolation(
                    issue.code,
                    "session plugin identity protocol is invalid",
                    issue.seq,
                )
            )
        expected_seq = 1
        open_turn: str | None = None
        open_step: str | None = None
        calls: dict[str, tuple[str, int]] = {}
        results: set[str] = set()
        closed_steps: set[str] = set()
        seen_seqs: set[int] = set()
        attempt_starts: dict[str, _AttemptStart] = {}
        attempt_ends: set[str] = set()
        open_attempt: str | None = None
        previous_attempt_end: dict[str, JsonValue] | None = None
        request_snapshots: dict[int, EventEnvelope] = {}
        snapshot_steps: dict[tuple[str, str], list[int]] = {}
        attempt_ordinals: dict[tuple[str, str], list[int]] = {}
        product_contexts: dict[tuple[int, int], str] = {}

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

            if event.type == PRODUCT_CONTEXT_SNAPSHOT:
                try:
                    context = parse_product_context_snapshot(event)
                except (TypeError, ValueError):
                    violations.append(
                        InvariantViolation(
                            "product-context-snapshot",
                            "model-visible Product context is not canonical",
                            event.seq,
                        )
                    )
                else:
                    previous = product_contexts.setdefault(
                        context.order_key, context.context_id
                    )
                    if previous != context.context_id:
                        violations.append(
                            InvariantViolation(
                                "product-context-order-unique",
                                "one Product context order names conflicting heads",
                                event.seq,
                            )
                        )

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

            if event.type == "request/snapshot":
                declared_turn = event.data.get("turn_id")
                declared_step = event.data.get("step_id")
                snapshot_valid = True
                if set(event.data) != REQUEST_SNAPSHOT_KEYS:
                    snapshot_valid = False
                    violations.append(
                        InvariantViolation(
                            "request-snapshot-keys",
                            "request snapshot does not use the current exact key set",
                            event.seq,
                        )
                    )
                if (
                    not isinstance(declared_turn, str)
                    or not declared_turn
                    or not isinstance(declared_step, str)
                    or not declared_step
                    or declared_turn != open_turn
                    or declared_step != open_step
                ):
                    snapshot_valid = False
                    violations.append(
                        InvariantViolation(
                            "request-snapshot-inside-step",
                            "request snapshot is not inside its declared open Turn and Step",
                            event.seq,
                        )
                    )
                source_seq = event.data.get("source_seq")
                composition_revision = event.data.get("composition_revision")
                if (
                    type(source_seq) is not int
                    or source_seq < 1
                    or source_seq >= event.seq
                    or not isinstance(composition_revision, str)
                    or not composition_revision
                    or event.composition_revision != composition_revision
                ):
                    snapshot_valid = False
                    violations.append(
                        InvariantViolation(
                            "request-snapshot-source-binding",
                            "request snapshot source boundary is invalid",
                            event.seq,
                        )
                    )
                if snapshot_valid:
                    try:
                        raw_composed = event.data["composed_request"]
                        raw_dispatch = event.data["dispatch_request"]
                        if not isinstance(raw_composed, dict) or not isinstance(
                            raw_dispatch, dict
                        ):
                            raise ValueError
                        composed = ModelRequest.from_dict(raw_composed)
                        dispatch = ModelRequest.from_dict(raw_dispatch)
                        if (
                            composed.to_dict() != raw_composed
                            or dispatch.to_dict() != raw_dispatch
                            or not dispatch_request_matches_composed(
                                composed, dispatch
                            )
                            or any(
                                dispatch.metadata.get(key) != expected
                                for key, expected in (
                                    (
                                        "session_id",
                                        event.stream_id.removeprefix("session:"),
                                    ),
                                    ("turn_id", declared_turn),
                                    ("step_id", declared_step),
                                    (
                                        "composition_revision",
                                        event.data["composition_revision"],
                                    ),
                                )
                            )
                        ):
                            raise ValueError
                        composed_fingerprint = event.data["composed_fingerprint"]
                        dispatch_fingerprint = event.data["dispatch_fingerprint"]
                        if (
                            not isinstance(composed_fingerprint, str)
                            or fingerprint(raw_composed) != composed_fingerprint
                            or not isinstance(dispatch_fingerprint, str)
                            or fingerprint(raw_dispatch) != dispatch_fingerprint
                        ):
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        snapshot_valid = False
                        violations.append(
                            InvariantViolation(
                                "request-dispatch-evidence",
                                "request snapshot has invalid dispatch evidence",
                                event.seq,
                            )
                        )
                if isinstance(declared_turn, str) and isinstance(declared_step, str):
                    snapshot_steps.setdefault(
                        (declared_turn, declared_step), []
                    ).append(event.seq)
                request_snapshots[event.seq] = event

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
                previous_attempt_end = None
            elif event.type == "model/attempt-start":
                attempt_id = attempt_identity(event.data)
                declared_turn = str(event.data.get("turn_id", ""))
                declared_step = str(event.data.get("step_id", ""))
                ordinal = event.data.get("ordinal")
                request_snapshot_seq = event.data.get("request_snapshot_seq")
                dispatch_fingerprint = event.data.get("dispatch_fingerprint")
                reservation_id = event.data.get("reservation_id")
                retry_wait = event.data.get("retry_wait_milliseconds")
                retry_code = event.data.get("retry_failure_code")
                retry_category = event.data.get("retry_failure_category")
                if set(event.data) != _ATTEMPT_START_KEYS:
                    violations.append(
                        InvariantViolation(
                            "attempt-start-keys",
                            "model attempt start does not use the current exact key set",
                            event.seq,
                        )
                    )
                if type(ordinal) is not int or ordinal < 1:
                    violations.append(
                        InvariantViolation(
                            "attempt-ordinal-valid",
                            "model attempt ordinal must be a positive integer",
                            event.seq,
                        )
                    )
                    ordinal = None
                if type(retry_wait) is not int or retry_wait < 0:
                    violations.append(
                        InvariantViolation(
                            "attempt-retry-wait-valid",
                            "model attempt retry wait must be non-negative milliseconds",
                            event.seq,
                        )
                    )
                if ordinal == 1:
                    if retry_wait != 0 or retry_code is not None or retry_category is not None:
                        violations.append(
                            InvariantViolation(
                                "attempt-retry-binding",
                                "first model attempt cannot claim retry evidence",
                                event.seq,
                            )
                        )
                elif ordinal is not None:
                    if (
                        not isinstance(retry_code, str)
                        or not retry_code
                        or not isinstance(retry_category, str)
                        or not retry_category
                        or previous_attempt_end is None
                        or previous_attempt_end.get("status") != "failed"
                        or previous_attempt_end.get("failure_code") != retry_code
                        or previous_attempt_end.get("failure_category") != retry_category
                    ):
                        violations.append(
                            InvariantViolation(
                                "attempt-retry-binding",
                                "retry attempt does not bind the preceding typed failure",
                                event.seq,
                            )
                        )
                if type(request_snapshot_seq) is not int:
                    violations.append(
                        InvariantViolation(
                            "attempt-request-snapshot-binding",
                            "model attempt must reference one earlier request snapshot",
                            event.seq,
                        )
                    )
                    request_snapshot_seq = None
                if not isinstance(dispatch_fingerprint, str) or not dispatch_fingerprint:
                    violations.append(
                        InvariantViolation(
                            "attempt-dispatch-fingerprint",
                            "model attempt has no valid dispatch fingerprint",
                            event.seq,
                        )
                    )
                    dispatch_fingerprint = None
                if reservation_id is not None and (
                    not isinstance(reservation_id, str) or not reservation_id
                ):
                    violations.append(
                        InvariantViolation(
                            "attempt-reservation-binding",
                            "model attempt reservation identity is invalid",
                            event.seq,
                        )
                    )
                    reservation_id = None
                if attempt_id is None:
                    violations.append(
                        InvariantViolation(
                            "attempt-id-present",
                            "model attempt start has no usable attempt_id",
                            event.seq,
                        )
                    )
                elif attempt_id in attempt_starts:
                    violations.append(
                        InvariantViolation(
                            "single-attempt-start",
                            f"model attempt {attempt_id} started more than once",
                            event.seq,
                        )
                    )
                else:
                    if ordinal is not None:
                        if isinstance(reservation_id, str):
                            try:
                                identity = ModelAttemptIdentity(
                                    session_id=event.stream_id.removeprefix("session:"),
                                    turn_id=declared_turn,
                                    step_id=declared_step,
                                    attempt_id=attempt_id,
                                    ordinal=ordinal,
                                )
                                expected_reservation = model_attempt_reservation_id(
                                    identity
                                )
                            except (TypeError, ValueError):
                                expected_reservation = None
                            if reservation_id != expected_reservation:
                                violations.append(
                                    InvariantViolation(
                                        "attempt-reservation-binding",
                                        "model attempt reservation does not match its identity",
                                        event.seq,
                                    )
                                )
                    attempt_starts[attempt_id] = _AttemptStart(
                        declared_turn,
                        declared_step,
                        event.seq,
                        event.event_id,
                        ordinal,
                        request_snapshot_seq,
                        dispatch_fingerprint,
                        reservation_id,
                    )
                if ordinal is not None:
                    key = (declared_turn, declared_step)
                    observed = attempt_ordinals.setdefault(key, [])
                    if ordinal != len(observed) + 1:
                        violations.append(
                            InvariantViolation(
                                "attempt-ordinal-contiguous",
                                "model attempt ordinals are not contiguous within the Step",
                                event.seq,
                            )
                        )
                    observed.append(ordinal)
                snapshot = (
                    request_snapshots.get(request_snapshot_seq)
                    if request_snapshot_seq is not None
                    else None
                )
                if (
                    snapshot is None
                    or snapshot.seq >= event.seq
                    or snapshot.data.get("turn_id") != declared_turn
                    or snapshot.data.get("step_id") != declared_step
                ):
                    violations.append(
                        InvariantViolation(
                            "attempt-request-snapshot-binding",
                            "model attempt does not reference its Step request snapshot",
                            event.seq,
                        )
                    )
                elif (
                    snapshot.data.get("dispatch_fingerprint")
                    != dispatch_fingerprint
                ):
                    violations.append(
                        InvariantViolation(
                            "attempt-dispatch-fingerprint",
                            "model attempt dispatch fingerprint differs from its snapshot",
                            event.seq,
                        )
                    )
                elif isinstance(snapshot.data.get("dispatch_request"), dict):
                    dispatch_request = snapshot.data["dispatch_request"]
                    if (
                        dispatch_request.get("provider")
                        != event.data.get("provider")
                        or dispatch_request.get("model") != event.data.get("model")
                    ):
                        violations.append(
                            InvariantViolation(
                                "attempt-provider-model-binding",
                                "model attempt provider/model differ from its dispatch request",
                                event.seq,
                            )
                        )
                # The payload does not get to declare its own scope: the attempt
                # must sit inside the turn and step that are really open here.
                if open_turn is None or open_step is None:
                    violations.append(
                        InvariantViolation(
                            "attempt-start-inside-step",
                            f"model attempt {attempt_id} started outside an open turn or step",
                            event.seq,
                        )
                    )
                elif declared_turn != open_turn or declared_step != open_step:
                    violations.append(
                        InvariantViolation(
                            "attempt-start-inside-step",
                            f"model attempt {attempt_id} claims turn {declared_turn} step "
                            f"{declared_step}, but turn {open_turn} step {open_step} is open",
                            event.seq,
                        )
                    )
                if open_attempt is not None:
                    violations.append(
                        InvariantViolation(
                            "single-open-attempt",
                            f"model attempt {attempt_id} started while {open_attempt} is open",
                            event.seq,
                        )
                    )
                if attempt_id is not None:
                    open_attempt = attempt_id
            elif event.type == "model/attempt-end":
                attempt_id = attempt_identity(event.data)
                if attempt_id is None:
                    violations.append(
                        InvariantViolation(
                            "attempt-id-present",
                            "model attempt end has no usable attempt_id",
                            event.seq,
                        )
                    )
                else:
                    if attempt_id in attempt_ends:
                        violations.append(
                            InvariantViolation(
                                "single-attempt-end",
                                f"model attempt {attempt_id} ended more than once",
                                event.seq,
                            )
                        )
                    start = attempt_starts.get(attempt_id)
                    if start is None:
                        violations.append(
                            InvariantViolation(
                                "attempt-end-has-start",
                                f"model attempt end {attempt_id} has no earlier start",
                                event.seq,
                            )
                        )
                    else:
                        for key, expected in (
                            ("ordinal", start.ordinal),
                            ("request_snapshot_seq", start.request_snapshot_seq),
                            ("dispatch_fingerprint", start.dispatch_fingerprint),
                            ("reservation_id", start.reservation_id),
                        ):
                            if event.data.get(key) != expected:
                                violations.append(
                                    InvariantViolation(
                                        "attempt-end-evidence-binding",
                                        f"model attempt {attempt_id} end changed {key}",
                                        event.seq,
                                    )
                                )
                        if start.turn_id != str(
                            event.data.get("turn_id", "")
                        ) or start.step_id != str(event.data.get("step_id", "")):
                            violations.append(
                                InvariantViolation(
                                    "attempt-end-same-scope",
                                    f"model attempt {attempt_id} ended in a different turn "
                                    "or step",
                                    event.seq,
                                )
                            )
                        # An append-only repair cannot be written back into the
                        # closed step, so a recovery end that names its start is
                        # allowed to arrive late. Everything else must close
                        # while its own step is still open.
                        repaired = (
                            event.data.get("recovered") is True
                            and event.causation_id is not None
                            and event.causation_id == start.event_id
                        )
                        if not repaired and (
                            open_turn != start.turn_id or open_step != start.step_id
                        ):
                            violations.append(
                                InvariantViolation(
                                    "attempt-end-inside-step",
                                    f"model attempt {attempt_id} ended after its turn or step "
                                    "was closed",
                                    event.seq,
                                )
                            )
                    attempt_ends.add(attempt_id)
                if open_attempt == attempt_id:
                    open_attempt = None
                previous_attempt_end = dict(event.data)
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
                # An attempt is only "running" inside its own step. Once the
                # step is closed an unpaired attempt is a missing-end problem,
                # reported once at the end, not a concurrency problem.
                open_attempt = None
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
                open_attempt = None

        # Checked over the whole stream rather than at step/end, so an
        # append-only recovery that closes an attempt after the step was already
        # closed counts as paired instead of failing forever.
        for attempt_id, start in attempt_starts.items():
            if attempt_id not in attempt_ends and start.step_id in closed_steps:
                violations.append(
                    InvariantViolation(
                        "attempt-has-end",
                        f"model attempt {attempt_id} in closed step {start.step_id} has no end",
                        start.seq,
                    )
                )

        for key, seqs in snapshot_steps.items():
            if len(seqs) != 1:
                violations.append(
                    InvariantViolation(
                        "single-request-snapshot",
                        f"Turn/Step {key[0]}/{key[1]} has {len(seqs)} request snapshots",
                        seqs[-1],
                    )
                )

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
