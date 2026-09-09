"""Conservative, event-derived detection of consecutive identical denied batches."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import canonical_json


@dataclass(frozen=True, slots=True)
class RepeatedDenialPolicy:
    warn_after: int = 2
    stop_after: int = 3

    def __post_init__(self) -> None:
        if (
            type(self.warn_after) is not int
            or type(self.stop_after) is not int
            or self.warn_after < 2
            or self.stop_after <= self.warn_after
        ):
            raise ValueError("repeated denial requires 2 <= warn_after < stop_after")

    def to_dict(self) -> dict[str, int]:
        return {"warn_after": self.warn_after, "stop_after": self.stop_after}


@dataclass(frozen=True, slots=True)
class RepeatedDenialState:
    count: int
    policy: RepeatedDenialPolicy


def repeated_denial_state(
    events: tuple[EventEnvelope, ...],
    *,
    turn_id: str,
) -> RepeatedDenialState | None:
    """Count completed steps, never attempts or individual calls in a parallel batch.

    Every counted result has already passed a fresh policy evaluation. An executed,
    incomplete, invalid, cancelled, changed or different batch breaks the streak.
    This does not infer unrecorded external permission state or cache permissions.
    """
    policy = None
    active = False
    step_id = None
    calls: dict[str, EventEnvelope] = {}
    results: list[EventEnvelope] = []
    previous = None
    count = 0
    for event in events:
        data = event.data
        if event.type == "turn/start":
            active = data.get("turn_id") == turn_id
            if active:
                raw = data.get("repeated_denial_policy")
                policy = None if raw is None else RepeatedDenialPolicy(**raw)
                previous, count = None, 0
            continue
        if not active or policy is None:
            continue
        if event.type == "turn/end":
            return None
        if event.type == "step/start":
            step_id = data.get("step_id")
            calls, results = {}, []
        elif event.type == "tool/call" and data.get("step_id") == step_id:
            calls[str(data["tool_call_id"])] = event
        elif event.type == "tool/result" and data.get("step_id") == step_id:
            results.append(event)
        elif event.type == "step/end" and data.get("step_id") == step_id:
            key = None
            if (
                data.get("reason") == "model_response"
                and calls
                and len(results) == len(calls)
                and {r.data["tool_call_id"] for r in results} == set(calls)
                and all(
                    r.data.get("status") == "denied" and r.data.get("effect_id") is None
                    for r in results
                )
            ):
                entries = []
                for result in results:
                    r = result.data
                    call = calls[str(r["tool_call_id"])].data
                    entries.append(
                        canonical_json(
                            {
                                "tool": call["tool_name"],
                                "arguments": call["arguments"],
                                "status": r["status"],
                                "error_type": r.get("error_type"),
                                "reason": r.get("content"),
                                "data": r.get("data"),
                            }
                        )
                    )
                key = (event.composition_revision, tuple(sorted(entries)))
            count = count + 1 if key is not None and key == previous else int(key is not None)
            previous = key
            step_id = None
    if policy is None or count == 0 or step_id is not None:
        return None
    return RepeatedDenialState(count, policy)
