"""Build and independently reconstruct model requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelRequest,
    dispatch_request_matches_composed,
    request_snapshot_keys,
    request_source_fields,
)
from traceh.concurrency import await_worker_convergence
from traceh.kernel.composition import CompositionSnapshot
from traceh.llm.token_meter import (
    TOKEN_MEASUREMENT,
    RequestTokenBudgetExceeded,
    RequestTokenMeter,
)
from traceh.session.context_input import read_context_input, render_context_message
from traceh.session.plugin_identity import parse_plugin_identities
from traceh.session.product_context import latest_product_context
from traceh.session.protocol import read_step_composition_event
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    request: ModelRequest
    source_seq: int
    fingerprint: str

    @property
    def is_summary(self) -> bool:
        return "summary_input_seq" in self.request.metadata


class RequestBuilder:
    def __init__(
        self,
        sessions: SessionService,
        surface: SurfaceProjector,
        *,
        token_meter: RequestTokenMeter | None = None,
    ) -> None:
        self.sessions = sessions
        self.surface = surface
        self.token_meter = token_meter

    async def prepare(
        self,
        *,
        session_id,
        turn_id,
        step_id,
        composition,
        active_composition,
        context_inputs,
        correlation_id,
        compaction=None,
        allow_compaction=False,
    ):
        """Freeze references and meter the complete draft before publishing a request.

        Any Step with room for continuation may compact closed older Turns.
        References are selected again after maintenance. Active Tool groups and
        outstanding disclosure lifetimes are preserved; a Turn has at most one
        semantic summary invocation chain.
        """
        context = await context_inputs.freeze(
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            composition=composition,
            active_composition=active_composition,
        )
        pending_summary = None
        if allow_compaction and compaction is not None and compaction.semantic_summary:
            from traceh.session.context_input import has_disclosure_requests

            events = await self.sessions.read_session(session_id)
            allow_compaction = not any(
                e.type == "summary/input" and e.data.get("turn_id") == turn_id for e in events
            ) and not has_disclosure_requests(context, events)
        if self.token_meter is not None and allow_compaction and compaction is not None:
            reference = render_context_message(context)

            def pressure(events):
                request = ModelRequest(
                    provider=composition.provider,
                    model=composition.model,
                    system_prompt=composition.system_prompt,
                    messages=(*self.surface.project(events), reference),
                    tools=composition.tools,
                    temperature=composition.temperature,
                    max_output_tokens=composition.max_output_tokens,
                )
                measured = self.token_meter.measure(request)
                return measured["input_tokens"] >= measured["trigger_tokens"]

            from traceh.session.compaction import CompactionError, PendingSemanticSummary
            from traceh.session.surface_replacement import SURFACE_COMPACTION_FAILED

            try:
                maintenance = await compaction.compact_before_turn(
                    session_id,
                    pressure=pressure,
                    trigger_digest=fingerprint(self.token_meter.policy.to_dict()),
                    defer_semantic=True,
                )
                if isinstance(maintenance, PendingSemanticSummary):
                    pending_summary = maintenance
            except CompactionError as error:
                await self.sessions.append_session(
                    session_id,
                    SURFACE_COMPACTION_FAILED,
                    {"method": "automatic", "code": error.code, "committed": error.committed},
                )
        if pending_summary is not None:
            from traceh.session.semantic_summary import SUMMARY_INPUT, summary_input_data

            data = summary_input_data(
                session_id=session_id,
                turn_id=turn_id,
                step_id=step_id,
                events=pending_summary.events,
                plan=pending_summary.plan,
                policy=pending_summary.policy,
                policy_digest=pending_summary.policy_digest,
                composition=composition,
                token_policy=self.token_meter.policy,
            )
            await self.sessions.append_session(
                session_id,
                SUMMARY_INPUT,
                data,
                expected_seq=data["observed_seq"],
                correlation_id=correlation_id,
                composition_revision=composition.revision,
            )
        else:
            if self.token_meter is not None:
                context = await context_inputs.freeze(
                    session_id=session_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    composition=composition,
                    active_composition=active_composition,
                    token_meter=self.token_meter,
                )
            data = context.to_dict()
            await self.sessions.append_context_input(
                session_id,
                data,
                expected_seq=data["observed_session_seq"],
                correlation_id=correlation_id,
            )
        event = await self.sessions.append_session(
            session_id,
            "composition/snapshot",
            composition.to_dict(),
            correlation_id=correlation_id,
            composition_revision=composition.revision,
        )
        built = await self.build(
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            composition=composition,
            through_seq=event.seq,
        )
        if self.token_meter is not None:
            events = await self.sessions.read_session(session_id)
            product = latest_product_context(
                tuple(item for item in events if item.seq <= built.source_seq)
            )
            measured = self.token_meter.measure(
                built.request,
                product_messages=len(product[1].messages)
                if product and not built.is_summary
                else 0,
                context_messages=0 if built.is_summary else 1,
            )
            write = asyncio.create_task(
                self.sessions.append_session(
                    session_id,
                    TOKEN_MEASUREMENT,
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "source_seq": built.source_seq,
                        "measurement": measured,
                    },
                    expected_seq=built.source_seq,
                    correlation_id=correlation_id,
                    composition_revision=composition.revision,
                )
            )
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await await_worker_convergence(write)
                raise
            if measured["over_limit"]:
                raise RequestTokenBudgetExceeded(
                    session_id=session_id, turn_id=turn_id, step_id=step_id
                )
        return built

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
    from traceh.session.semantic_summary import read_summary_input, summary_model_request

    summary = read_summary_input(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        through_seq=through_seq,
        composition=stored_composition,
    )
    if summary is not None:
        request = summary_model_request(events, event=summary[0], composition=stored_composition)
        return BuiltRequest(request, through_seq, fingerprint(request.to_dict()))
    context_event, context = read_context_input(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        through_seq=through_seq,
        composition=stored_composition,
    )
    # The current reference follows complete Tool groups and prior conversation.
    # Surface itself never learns to retain these request-only bytes.
    messages = (*surface.project(events, through_seq=through_seq), render_context_message(context))
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
        or set(request_event.data) != request_snapshot_keys(request_event.data)
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
    for key in ("composition_revision", *request_source_fields(rebuilt.request.metadata)):
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
        if set(event.data) != request_snapshot_keys(event.data):
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
            if not isinstance(expected_composed, str) or not isinstance(expected_dispatch, str):
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


def validate_token_measurements(events):
    """Audit optional metering evidence against the same reconstructed request."""
    from traceh.llm.token_meter import validate_measurement

    seen = set()
    for index, event in enumerate(events):
        if event.type != TOKEN_MEASUREMENT:
            continue
        data = event.data
        if set(data) != {"turn_id", "step_id", "source_seq", "measurement"}:
            raise ValueError("token-measurement-envelope-invalid")
        identity = (data["turn_id"], data["step_id"])
        if (
            identity in seen
            or type(data["source_seq"]) is not int
            or data["source_seq"] != event.seq - 1
        ):
            raise ValueError("token-measurement-source-invalid")
        seen.add(identity)
        built = build_request_from_events(
            tuple(events[:index]),
            SurfaceProjector(),
            session_id=event.stream_id.removeprefix("session:"),
            turn_id=data["turn_id"],
            step_id=data["step_id"],
            through_seq=data["source_seq"],
        )
        measured = validate_measurement(data["measurement"], built.request)
        product = latest_product_context(tuple(events[:index]))
        if measured["product_messages"] != (
            len(product[1].messages) if product and not built.is_summary else 0
        ) or measured["context_messages"] != (0 if built.is_summary else 1):
            raise ValueError("token-measurement-partition-invalid")
        if event.composition_revision != built.request.metadata["composition_revision"]:
            raise ValueError("token-measurement-composition-invalid")
        snapshots = [
            e
            for e in events
            if e.type == "request/snapshot" and (e.data["turn_id"], e.data["step_id"]) == identity
        ]
        if any(
            e.seq <= event.seq
            or measured["over_limit"]
            or e.data["composed_fingerprint"] != measured["request_fingerprint"]
            for e in snapshots
        ):
            raise ValueError("token-measurement-dispatch-invalid")


__all__ = [
    "BuiltRequest",
    "REQUEST_SNAPSHOT_KEYS",
    "RequestBuilder",
    "build_request_from_events",
    "composition_from_event",
    "reconstruct_request",
    "verify_request_snapshots",
]
