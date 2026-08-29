"""Build and independently reconstruct model requests."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelRequest,
    ToolSchema,
    dispatch_request_matches_composed,
)
from traceh.kernel.composition import CompositionSnapshot
from traceh.session.plugin_identity import parse_plugin_identities
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
        messages = self.surface.project(events, through_seq=through_seq)
        request = ModelRequest(
            provider=composition.provider,
            model=composition.model,
            system_prompt=composition.system_prompt,
            messages=messages,
            tools=composition.tools,
            temperature=composition.temperature,
            max_output_tokens=composition.max_output_tokens,
            metadata={
                "session_id": session_id,
                "turn_id": turn_id,
                "step_id": step_id,
                "composition_revision": composition.revision,
            },
        )
        return BuiltRequest(request, through_seq, fingerprint(request.to_dict()))


def composition_from_event(event: EventEnvelope) -> CompositionSnapshot:
    data = event.data
    raw_tools = data.get("tools", [])
    if not isinstance(raw_tools, list):
        raise ValueError("composition tools must be a list")
    tools = tuple(ToolSchema.from_dict(item) for item in raw_tools if isinstance(item, dict))
    # Plugin identities are part of what a step was composed from, so replay must
    # rebuild them rather than assume none. Dropping them made every reconstructed
    # composition claim a plugin-free runtime.
    raw_plugins = data.get("plugins", [])
    plugins = parse_plugin_identities(
        raw_plugins,
        allow_core=True,
        error_code="composition-plugins-valid",
        seq=event.seq,
    )
    return CompositionSnapshot(
        revision=str(data["revision"]),
        provider=str(data["provider"]),
        model=str(data["model"]),
        system_prompt=str(data.get("system_prompt", "")),
        tools=tools,
        plugins=plugins,
        policies=tuple(str(item) for item in data.get("policies", []) if isinstance(item, str)),
        tool_middlewares=tuple(
            str(item)
            for item in data.get("tool_middlewares", [])
            if isinstance(item, str)
        ),
        temperature=(float(data["temperature"]) if data.get("temperature") is not None else None),
        max_output_tokens=(
            int(data["max_output_tokens"])
            if data.get("max_output_tokens") is not None
            else None
        ),
    )


async def reconstruct_request(
    sessions: SessionService,
    surface: SurfaceProjector,
    session_id: str,
    request_event: EventEnvelope,
) -> BuiltRequest:
    source_seq = int(request_event.data["source_seq"])
    composition_revision = str(request_event.data["composition_revision"])
    events = await sessions.read_session(session_id)
    composition_event = next(
        (
            event
            for event in reversed(events)
            if event.seq <= source_seq
            and event.type == "composition/snapshot"
            and str(event.data.get("revision")) == composition_revision
        ),
        None,
    )
    if composition_event is None:
        raise ValueError(f"composition snapshot not found: {composition_revision}")
    composition = composition_from_event(composition_event)
    messages = surface.project(events, through_seq=source_seq)
    open_turn_id: str | None = None
    open_step_id: str | None = None
    for event in events:
        if event.seq > source_seq:
            break
        if event.type == "turn/start":
            open_turn_id = str(event.data.get("turn_id"))
        elif event.type == "turn/end":
            open_turn_id = None
        elif event.type == "step/start":
            open_step_id = str(event.data.get("step_id"))
        elif event.type == "step/end":
            open_step_id = None
    if open_turn_id is None or open_step_id is None:
        raise ValueError("request source boundary is not inside an open Turn and Step")
    request = ModelRequest(
        provider=composition.provider,
        model=composition.model,
        system_prompt=composition.system_prompt,
        messages=messages,
        tools=composition.tools,
        temperature=composition.temperature,
        max_output_tokens=composition.max_output_tokens,
        metadata={
            "session_id": session_id,
            "turn_id": open_turn_id,
            "step_id": open_step_id,
            "composition_revision": composition.revision,
        },
    )
    return BuiltRequest(request, source_seq, fingerprint(request.to_dict()))


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
    "composition_from_event",
    "reconstruct_request",
    "verify_request_snapshots",
]
