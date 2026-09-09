"""Read-only explanation of an identified, durably rejected request."""

from dataclasses import dataclass

from traceh.llm.token_meter import TOKEN_MEASUREMENT, RequestTokenBudgetExceeded
from traceh.runtime.request_builder import validate_token_measurements
from traceh.session.service import SessionService


@dataclass(frozen=True, slots=True)
class ContextPressureView:
    input_tokens: int
    input_limit: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    parts: tuple[tuple[str, int], ...]
    maintenance: tuple[tuple[str, int], ...]
    maintenance_failures: int
    reference_exclusions: int
    summary_request: bool


async def read_context_pressure(
    sessions: SessionService, error: RequestTokenBudgetExceeded
) -> ContextPressureView:
    events = await sessions.read_session(error.session_id)
    # Use the existing canonical proof, not a second implementation of token
    # accounting. Later Turns must not replace the failed Step's explanation.
    validate_token_measurements(events)
    event = next(
        e
        for e in events
        if e.type == TOKEN_MEASUREMENT
        and e.data["turn_id"] == error.turn_id
        and e.data["step_id"] == error.step_id
    )
    measured = event.data["measurement"]
    if not measured["over_limit"]:
        raise ValueError("context-pressure-not-rejected")
    start = next(
        e.seq for e in events if e.type == "turn/start" and e.data["turn_id"] == error.turn_id
    )
    current = tuple(e for e in events if start <= e.seq < event.seq)
    methods = ("tool-fold", "automatic", "semantic")
    maintenance = tuple(
        (
            method,
            sum(e.type == "surface/replace" and e.data.get("method") == method for e in current),
        )
        for method in methods
    )
    context = next(
        (
            e.data
            for e in reversed(current)
            if e.type == "context/input" and e.data["step_id"] == error.step_id
        ),
        None,
    )
    exclusions = sum(
        item.get("reason") == "token-budget-excluded"
        for item in (context or {}).get("exclusions", [])
    )
    return ContextPressureView(
        input_tokens=measured["input_tokens"],
        input_limit=measured["input_limit"],
        output_reserve_tokens=measured["output_reserve_tokens"],
        safety_margin_tokens=measured["safety_margin_tokens"],
        parts=tuple(measured["parts"].items()),
        maintenance=maintenance,
        maintenance_failures=sum(e.type == "surface/compaction-failed" for e in current),
        reference_exclusions=exclusions,
        summary_request=measured["context_messages"] == 0,
    )
