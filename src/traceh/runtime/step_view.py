"""Optional host-owned Step view; no Product or role logic lives here."""

from dataclasses import dataclass, replace
from typing import Protocol

from traceh.runtime.verification import CompletionVerifier
from traceh.session.request_view import VIEW_EVENT, RequestView, view_composition, view_data


@dataclass(frozen=True, slots=True)
class StepViewSelection:
    view: RequestView
    verifier: CompletionVerifier | None = None


class StepViewPolicy(Protocol):
    async def select(
        self, events, composition, *, turn_id: str, step_id: str
    ) -> StepViewSelection: ...


async def freeze_step_view(sessions, policy, active, *, session_id, turn_id, step_id):
    events = await sessions.read_session(session_id)
    selection = await policy.select(events, active.snapshot, turn_id=turn_id, step_id=step_id)
    view = selection.view
    if selection.verifier is not None and active.verifier is not None:
        raise ValueError("request-view-verifier-conflict")
    selected = view_composition(active.snapshot, view)
    await sessions.append_session(
        session_id,
        VIEW_EVENT,
        view_data(events, active.snapshot, view, turn_id=turn_id, step_id=step_id),
        expected_seq=events[-1].seq,
        composition_revision=selected.revision,
    )
    return replace(
        active, snapshot=selected,
        verifier=selection.verifier if selection.verifier is not None else active.verifier,
    )
