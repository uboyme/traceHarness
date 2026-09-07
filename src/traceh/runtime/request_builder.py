"""Build and independently reconstruct model requests."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelRequest,
    dispatch_request_matches_composed,
)
from traceh.kernel.composition import CompositionSnapshot
from traceh.session.context_input import read_context_input, render_context_message
from traceh.session.plugin_identity import parse_plugin_identities
from traceh.session.protocol import read_step_composition_event
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    request: ModelRequest
    source_seq: int
    fingerprint: str


class RequestBuilder:
    def __init__(self, sessions: SessionService, surface: SurfaceProjector) -> None:
        self.sessions = sessions
        self.surface = surface

    async def build(
        self,
        *,
        session_id: str,
        turn_id: str,
        step_id: str,
        composition: CompositionSnapshot,
        through_seq: int,
    ) -> BuiltRequest:
        events = await self.sessions.read_session(session_id)
        return build_request_from_events(
            events,
            self.surface,
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            through_seq=through_seq,
            composition=composition,
        )


def composition_from_event(event: EventEnvelope) -> CompositionSnapshot:
    data = event.data
    # Plugin identities are part of what a step was composed from, so replay must
    # rebuild them rather than assume none. Dropping them made every reconstructed
    # composition claim a plugin-free runtime.
    parse_plugin_identities(
        data.get("plugins"),
        allow_core=True,
        error_code="composition-plugins-valid",
        seq=event.seq,
    )
    composition = CompositionSnapshot.from_dict(data)
    if event.composition_revision != composition.revision:
        raise ValueError("composition-envelope-revision-mismatch")
    return composition


def build_request_from_events(
    events: tuple[EventEnvelope, ...],
    surface: SurfaceProjector,
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    through_seq: int,
    composition: CompositionSnapshot | None = None,
) -> BuiltRequest:
    """One pure request construction for dispatch, replay and verification."""

    composition_event = read_step_composition_event(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        through_seq=through_seq,
    )
    stored_composition = composition_from_event(composition_event)
    if composition is not None and canonical_json(composition.to_dict()) != canonical_json(
        stored_composition.to_dict()
    ):
        raise ValueError("request-composition-binding-mismatch")
    context_event, context = read_context_input(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        through_seq=through_seq,
        composition=stored_composition,
    )
    # Context is request-only and always precedes the complete Surface, even on
    # Tool continuation. The Surface itself never learns to retain its bytes.
    messages = (render_context_message(context), *surface.project(events, through_seq=through_seq))
    request = ModelRequest(
        provider=stored_composition.provider,
        model=stored_composition.model,
        system_prompt=stored_composition.system_prompt,
        messages=messages,
        tools=stored_composition.tools,
        temperature=stored_composition.temperature,
        max_output_tokens=stored_composition.max_output_tokens,
        metadata={
            "session_id": session_id,
            "turn_id": turn_id,
            "step_id": step_id,
            "composition_revision": stored_composition.revision,
            "context_input_seq": context_event.seq,
            "context_input_digest": context.context_digest,
        },
    )
    return BuiltRequest(request, through_seq, fingerprint(request.to_dict()))


async def reconstruct_request(
    sessions: SessionService,
    surface: SurfaceProjector,
    session_id: str,
    request_event: EventEnvelope,
) -> BuiltRequest:
    if (
        request_event.type != "request/snapshot"
        or request_event.stream_id != f"session:{session_id}"
        or set(request_event.data) != REQUEST_SNAPSHOT_KEYS
    ):
        raise ValueError("request-snapshot-payload-invalid")
    source_seq = request_event.data["source_seq"]
    turn_id = request_event.data["turn_id"]
    step_id = request_event.data["step_id"]
    if (
        type(source_seq) is not int
        or source_seq >= request_event.seq
        or not isinstance(turn_id, str)
        or not isinstance(step_id, str)
    ):
        raise ValueError("request-snapshot-source-binding")
    events = await sessions.read_session(session_id)
    rebuilt = build_request_from_events(
        events,
        surface,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        through_seq=source_seq,
    )
    for key in ("composition_revision", "context_input_seq", "context_input_digest"):
        if canonical_json(request_event.data[key]) != canonical_json(rebuilt.request.metadata[key]):
            raise ValueError("request-context-binding-mismatch")
    if request_event.composition_revision != rebuilt.request.metadata["composition_revision"]:
        raise ValueError("request-composition-binding-mismatch")
    return rebuilt


async def verify_request_snapshots(
    sessions: SessionService,
    surface: SurfaceProjector,
    session_id: str,
) -> tuple[dict[str, JsonValue], ...]:
    violations: list[dict[str, JsonValue]] = []
    events = await sessions.read_session(session_id)
    for event in events:
        if event.type != "request/snapshot":
            continue
        if set(event.data) != REQUEST_SNAPSHOT_KEYS:
            violations.append(
                {
                    "seq": event.seq,
                    "code": "request-snapshot-keys-invalid",
                }
            )
            continue
        try:
            rebuilt = await reconstruct_request(sessions, surface, session_id, event)
            raw_composed = event.data["composed_request"]
            raw_dispatch = event.data["dispatch_request"]
            if not isinstance(raw_composed, dict) or not isinstance(raw_dispatch, dict):
                raise ValueError
            composed = ModelRequest.from_dict(raw_composed)
            dispatch = ModelRequest.from_dict(raw_dispatch)
            canonical_composed = composed.to_dict()
            canonical_dispatch = dispatch.to_dict()
            if raw_composed != canonical_composed or raw_dispatch != canonical_dispatch:
                raise ValueError
            expected_composed = event.data["composed_fingerprint"]
            expected_dispatch = event.data["dispatch_fingerprint"]
            if not isinstance(expected_composed, str) or not isinstance(
                expected_dispatch, str
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            violations.append(
                {
                    "seq": event.seq,
                    "code": "request-snapshot-payload-invalid",
                }
            )
            continue

        actual_dispatch = fingerprint(canonical_dispatch)
        if (
            rebuilt.fingerprint != expected_composed
            or fingerprint(canonical_composed) != expected_composed
            or rebuilt.request.to_dict() != canonical_composed
        ):
            violations.append(
                {
                    "seq": event.seq,
                    "code": "request-composed-fingerprint-mismatch",
                    "expected": expected_composed,
                    "actual": rebuilt.fingerprint,
                }
            )
        if actual_dispatch != expected_dispatch:
            violations.append(
                {
                    "seq": event.seq,
                    "code": "request-dispatch-fingerprint-mismatch",
                    "expected": expected_dispatch,
                    "actual": actual_dispatch,
                }
            )

        if not dispatch_request_matches_composed(composed, dispatch):
            violations.append(
                {
                    "seq": event.seq,
                    "code": "request-dispatch-not-derived-from-composed",
                }
            )
    return tuple(violations)


__all__ = [
    "BuiltRequest",
    "REQUEST_SNAPSHOT_KEYS",
    "RequestBuilder",
    "build_request_from_events",
    "composition_from_event",
    "reconstruct_request",
    "verify_request_snapshots",
]
