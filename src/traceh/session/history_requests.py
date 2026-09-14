"""History request identities derived from the existing Session event log.

This module has no writer or pending-request cache. Disclosure is proved from
an earlier frozen request, and eligibility is derived from Turn/Step order.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.history import HistoryCursor, HistoryPageRequest, HistoryReadPolicy
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelRequest,
    dispatch_request_matches_composed,
)
from traceh.api.tools import ToolExecutionContext
from traceh.session.history import read_history
from traceh.session.protocol import read_step_composition_event, require_session_protocol

HISTORY_TOOL_NAME = "request_history_page"
_HOST_KEYS = frozenset(
    {
        "format",
        "session_id",
        "turn_id",
        "step_id",
        "user_message_ref",
        "block_id",
        "cursor",
        "requested_tier",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "format",
        "status",
        "session_id",
        "turn_id",
        "source_step_id",
        "tool_call_id",
        "block_id",
        "cursor",
        "requested_tier",
        "target_rule",
    }
)


class HistoryRequestError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HistoryRequestWriteError(RuntimeError):
    code = "history-request-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        super().__init__(self.code)
        self.committed = committed


@dataclass(frozen=True, slots=True)
class EligibleHistoryRequest:
    request: HistoryPageRequest
    request_ref: dict[str, JsonValue]


def event_ref(event: EventEnvelope) -> dict[str, JsonValue]:
    return {
        "stream_id": event.stream_id,
        "seq": event.seq,
        "type": event.type,
        "event_id": str(event.event_id),
        "digest": fingerprint(event.to_dict()),
    }


def _fail(code: str = "history-request-binding-mismatch") -> None:
    raise HistoryRequestError(code)


def _prefix(events: tuple[EventEnvelope, ...], session_id: str) -> None:
    require_session_protocol(events, session_id=session_id)
    if not events or any(
        event.seq != number or event.stream_id != f"session:{session_id}"
        for number, event in enumerate(events, 1)
    ):
        _fail()


def _step(
    events: tuple[EventEnvelope, ...],
    turn_id: str,
    step_id: str,
) -> tuple[EventEnvelope, tuple[EventEnvelope, ...]]:
    open_turn = open_step = None
    starts: list[EventEnvelope] = []
    for event in events:
        if event.type == "turn/start":
            open_turn = event.data.get("turn_id")
            open_step = None
        elif event.type == "turn/end":
            open_turn = open_step = None
        elif event.type == "step/start":
            if event.data.get("turn_id") != open_turn:
                _fail()
            open_step = event.data.get("step_id")
            if open_turn == turn_id:
                starts.append(event)
        elif event.type == "step/end":
            open_step = None
    if open_turn != turn_id or open_step != step_id or not starts:
        _fail()
    if starts[-1].data.get("step_id") != step_id:
        _fail()
    return starts[-1], tuple(starts)


def _request(raw: dict) -> HistoryPageRequest:
    try:
        return HistoryPageRequest.from_dict(
            {
                "block_id": raw["block_id"],
                "cursor": raw["cursor"],
                "requested_tier": raw["requested_tier"],
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HistoryRequestError("history-request-invalid") from error


def _context_for_request(
    events: tuple[EventEnvelope, ...],
    event: EventEnvelope,
) -> dict:
    # Do not call the complete Context source validator here: it consumes
    # requests itself. Prove this disclosure boundary, then derive its cursor
    # from canonical History sources below, without a recursive request replay.
    from traceh.runtime.request_builder import composition_from_event
    from traceh.session.context_input import parse_context_input, render_context_message

    try:
        data = event.data
        if set(data) != REQUEST_SNAPSHOT_KEYS:
            _fail()
        source_seq = data["source_seq"]
        context_seq = data["context_input_seq"]
        if (
            type(source_seq) is not int
            or type(context_seq) is not int
            or not 0 < context_seq < source_seq < event.seq
        ):
            _fail()
        source = read_step_composition_event(
            events,
            session_id=event.stream_id.removeprefix("session:"),
            turn_id=data["turn_id"],
            step_id=data["step_id"],
            through_seq=source_seq,
        )
        composition = composition_from_event(source)
        context_event = events[context_seq - 1]
        if context_event.type != "context/input":
            _fail()
        snapshot = parse_context_input(context_event.data)
        context = snapshot.to_dict()
        if (
            context["session_id"] != event.stream_id.removeprefix("session:")
            or context["turn_id"] != data["turn_id"]
            or context["step_id"] != data["step_id"]
            or context["context_digest"] != data["context_input_digest"]
            or context["composition_revision"] != composition.revision
            or data["composition_revision"] != composition.revision
            or event.composition_revision != composition.revision
            or context_event.composition_revision != composition.revision
            or context["observed_session_seq"] >= context_seq
        ):
            _fail()
        step, _ = _step(events[: context_seq - 1], data["turn_id"], data["step_id"])
        if context["observed_session_seq"] < step.seq:
            _fail()
        context_events = [
            item for item in events[step.seq : source_seq] if item.type == "context/input"
        ]
        if context_events != [context_event]:
            _fail()
        composed = ModelRequest.from_dict(data["composed_request"])
        dispatch = ModelRequest.from_dict(data["dispatch_request"])
        for key, request in (("composed", composed), ("dispatch", dispatch)):
            if (
                canonical_json(data[f"{key}_request"]) != canonical_json(request.to_dict())
                or fingerprint(request.to_dict()) != data[f"{key}_fingerprint"]
            ):
                _fail()
        if not dispatch_request_matches_composed(composed, dispatch):
            _fail()
        expected_metadata = {
            "session_id": context["session_id"],
            "turn_id": data["turn_id"],
            "step_id": data["step_id"],
            "composition_revision": composition.revision,
            "context_input_seq": context_seq,
            "context_input_digest": snapshot.context_digest,
        }
        from traceh.session.request_view import evidence_messages, read_view
        from traceh.session.surface import SurfaceProjector

        view = read_view(
            events,
            session_id=context["session_id"],
            turn_id=data["turn_id"],
            step_id=data["step_id"],
            through_seq=source_seq,
        )
        context_matches = composed.messages and composed.messages[-1] == render_context_message(
            snapshot
        )
        if view is not None:
            if view[2].to_dict() != composition.to_dict():
                _fail()
            if view[1].input_mode == "evidence":
                context_matches = composed.messages == evidence_messages(
                    view[3],
                    SurfaceProjector(),
                    turn_id=data["turn_id"],
                    context=render_context_message(snapshot),
                )
        if (
            canonical_json(composed.metadata) != canonical_json(expected_metadata)
            or not composed.messages
            or not context_matches
        ):
            _fail()
        return context
    except (IndexError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, HistoryRequestError):
            raise
        raise HistoryRequestError("history-disclosure-invalid") from error


def _discloses(
    events: tuple[EventEnvelope, ...],
    request_event: EventEnvelope,
    request: HistoryPageRequest,
    policy: HistoryReadPolicy,
) -> bool:
    context = _context_for_request(events, request_event)
    raw_policy = context["policy"]["config"].get("history")
    if raw_policy is None:
        return False
    stored_policy = HistoryReadPolicy.from_dict(raw_policy)
    if stored_policy.digest != policy.digest or request.cursor.policy_digest != policy.digest:
        return False
    from traceh.session.reference_search import history_discloses

    if history_discloses(events, request_event, request, context):
        return True
    history = read_history(
        events,
        session_id=context["session_id"],
        through_seq=context["observed_session_seq"],
        policy=policy,
    )
    for block in context["blocks"]:
        if block["kind"] != "history" or block["id"] != request.block_id:
            continue
        source = history.resolve(request.block_id)
        if (
            block["version"] != source.version
            or canonical_json(block["source_refs"]) != canonical_json(source.source_refs)
            or block["provenance"]["observed_through_seq"] != source.observed_through_seq
            or block["provenance"]["observed_at"] != source.observed_at
        ):
            _fail("history-disclosure-invalid")
        if block["tier"] in {"directory", "summary"}:
            if source.block_id not in {item.block_id for item in history.directory()}:
                _fail("history-disclosure-invalid")
            expected_body = (
                source.summary
                if block["tier"] == "summary"
                else canonical_json(
                    {
                        "block_id": source.block_id,
                        "observed_through_seq": source.observed_through_seq,
                        "observed_at": source.observed_at,
                        "cursor": source.first_cursor.to_dict(),
                        "original_bytes": history.original_bytes(source.block_id),
                    }
                )
            )
            if block["body"] != expected_body:
                _fail("history-disclosure-invalid")
            disclosed = source.first_cursor.to_dict()
        else:
            page = block["provenance"]["page"]
            cursor = HistoryCursor(
                block_id=request.block_id,
                policy_digest=policy.digest,
                index=page["index"],
            )
            raw = history.read_page(block_id=request.block_id, cursor=cursor)
            if canonical_json(page) != canonical_json(raw.page) or block["body"] != raw.body:
                _fail("history-disclosure-invalid")
            _raw_request_source(events, context, block, cursor)
            disclosed = raw.page["next_cursor"]
        if canonical_json(disclosed) == canonical_json(request.cursor.to_dict()):
            return True
    return False


def _raw_request_source(
    events: tuple[EventEnvelope, ...],
    context: dict,
    block: dict,
    cursor: HistoryCursor,
) -> None:
    """Prove the original grant and uninterrupted admissions without recursion.

    Public callers validate accepted events in sequence. The shared collector
    only derives lifetime from those grants and past Context admissions; it
    does not recursively validate the request's earlier source again.
    """
    ref = block["provenance"]["request_ref"]
    prefix = events[: context["observed_session_seq"]]
    if (
        type(ref) is not dict
        or type(ref.get("seq")) is not int
        or not 0 < ref["seq"] <= len(prefix)
    ):
        _fail("history-disclosure-invalid")
    source = prefix[ref["seq"] - 1]
    if canonical_json(event_ref(source)) != canonical_json(ref):
        _fail("history-disclosure-invalid")
    policy = HistoryReadPolicy.from_dict(context["policy"]["config"]["history"])
    groups = _collect_history_requests(
        prefix,
        session_id=context["session_id"],
        turn_id=context["turn_id"],
        step_id=context["step_id"],
        policy=policy,
    )
    if not any(
        candidate.request.cursor == cursor and matches_block(candidate, block)
        for candidate in groups.all
    ):
        _fail("history-disclosure-invalid")


def _policy_for(
    events: tuple[EventEnvelope, ...], request: HistoryPageRequest
) -> HistoryReadPolicy:
    from traceh.session.context_input import parse_context_input

    for event in reversed(events):
        if event.type != "context/input":
            continue
        context = parse_context_input(event.data).to_dict()
        raw = context["policy"]["config"].get("history")
        if raw is not None:
            policy = HistoryReadPolicy.from_dict(raw)
            if policy.digest == request.cursor.policy_digest:
                return policy
    _fail("history-request-not-disclosed")


def _user_source(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    user_message_ref: dict,
    request: HistoryPageRequest,
    policy: HistoryReadPolicy,
) -> None:
    step, starts = _step(events, turn_id, step_id)
    if len(starts) != 1:
        _fail("history-user-request-not-first-step")
    users = [event for event in events if event.type == "user/message" and event.seq > step.seq]
    if (
        len(users) != 1
        or users[0].data.get("turn_id") != turn_id
        or users[0].data.get("step_id") != step_id
        or canonical_json(event_ref(users[0])) != canonical_json(user_message_ref)
        or any(
            event.type in {"context/input", "composition/snapshot", "runtime/cancel-requested"}
            for event in events[step.seq :]
        )
    ):
        _fail()
    candidates = [
        event
        for event in events[: users[0].seq - 1]
        if event.type == "request/snapshot" and "context_input_seq" in event.data
    ]
    if not any(_discloses(events, event, request, policy) for event in reversed(candidates)):
        _fail("history-request-not-disclosed")
    read_history(events, session_id=session_id, through_seq=users[0].seq, policy=policy).read_page(
        block_id=request.block_id,
        cursor=request.cursor,
    )


def validate_user_requests(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    user_message_ref: dict,
    requests: tuple[HistoryPageRequest, ...],
    policy: HistoryReadPolicy,
) -> tuple[dict[str, JsonValue], ...]:
    _prefix(events, session_id)
    validate_history_request_events(events)
    if type(requests) is not tuple or not requests or len(requests) > policy.max_requests:
        _fail("history-request-limit")
    step, _ = _step(events, turn_id, step_id)
    if any(event.type == "history/requested" for event in events[step.seq :]):
        _fail("history-user-request-already-recorded")
    payloads: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    for request in requests:
        if type(request) is not HistoryPageRequest:
            _fail("history-request-invalid")
        _user_source(
            events,
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            user_message_ref=user_message_ref,
            request=request,
            policy=policy,
        )
        identity = canonical_json(request.cursor.to_dict())
        if identity in seen:
            continue
        seen.add(identity)
        payloads.append(
            {
                "format": 1,
                "session_id": session_id,
                "turn_id": turn_id,
                "step_id": step_id,
                "user_message_ref": user_message_ref,
                **request.to_dict(),
            }
        )
    return tuple(payloads)


def _model_source(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    tool_call_id: str,
    request: HistoryPageRequest,
    policy: HistoryReadPolicy,
) -> EventEnvelope:
    calls = [
        event
        for event in events
        if event.type == "tool/call" and event.data.get("tool_call_id") == tool_call_id
    ]
    if len(calls) != 1:
        _fail()
    call = calls[0]
    before_call = events[: call.seq]
    step, _ = _step(before_call, turn_id, step_id)
    if (
        call.data.get("turn_id") != turn_id
        or call.data.get("step_id") != step_id
        or call.data.get("tool_name") != HISTORY_TOOL_NAME
        or canonical_json(call.data.get("arguments")) != canonical_json(request.to_dict())
    ):
        _fail()
    snapshots = [
        event for event in events[step.seq : call.seq - 1] if event.type == "request/snapshot"
    ]
    if len(snapshots) != 1:
        _fail("history-request-not-disclosed")
    source = snapshots[0]
    assistants = [
        event
        for event in events[source.seq : call.seq - 1]
        if event.type == "assistant/message"
        and event.data.get("turn_id") == turn_id
        and event.data.get("step_id") == step_id
    ]
    expected_call = {"id": tool_call_id, "name": HISTORY_TOOL_NAME, "arguments": request.to_dict()}
    if not any(expected_call in event.data.get("tool_calls", []) for event in assistants):
        _fail("history-request-has-no-model-call")
    if not _discloses(events, source, request, policy):
        _fail("history-request-not-disclosed")
    context = _context_for_request(events, source)
    composition = events[source.data["source_seq"] - 1]
    if not any(tool.get("name") == HISTORY_TOOL_NAME for tool in composition.data["tools"]):
        _fail("history-request-tool-not-exposed")
    if call.composition_revision != context["composition_revision"]:
        _fail()
    return call


def validate_model_history_request(
    events: tuple[EventEnvelope, ...],
    *,
    context: ToolExecutionContext,
    request: HistoryPageRequest,
    policy: HistoryReadPolicy,
) -> dict[str, JsonValue]:
    from traceh.session.reference_requests import RECEIPT_FORMAT, TARGET_RULE

    _prefix(events, context.session_id)
    validate_history_request_events(events)
    step, _ = _step(events, context.turn_id, context.step_id)
    if any(event.type == "runtime/cancel-requested" for event in events[step.seq :]):
        _fail("history-request-expired")
    call = _model_source(
        events,
        session_id=context.session_id,
        turn_id=context.turn_id,
        step_id=context.step_id,
        tool_call_id=context.tool_call_id,
        request=request,
        policy=policy,
    )
    _model_page(events, call, request, policy)
    return {
        "format": RECEIPT_FORMAT,
        "status": "accepted",
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "source_step_id": context.step_id,
        "tool_call_id": context.tool_call_id,
        **request.to_dict(),
        "target_rule": TARGET_RULE,
    }


def _model_page(
    events: tuple[EventEnvelope, ...],
    call: EventEnvelope,
    request: HistoryPageRequest,
    policy: HistoryReadPolicy,
) -> None:
    calls = [
        event
        for event in events
        if event.type == "tool/call"
        and event.data.get("turn_id") == call.data["turn_id"]
        and event.data.get("step_id") == call.data["step_id"]
        and event.data.get("tool_name") == HISTORY_TOOL_NAME
    ]
    if len(calls) > policy.max_requests:
        _fail("history-request-limit")
    read_history(
        events,
        session_id=call.stream_id.removeprefix("session:"),
        through_seq=call.seq,
        policy=policy,
    ).read_page(block_id=request.block_id, cursor=request.cursor)


def _validate_host_event(
    events: tuple[EventEnvelope, ...], event: EventEnvelope
) -> HistoryPageRequest:
    data = event.data
    if set(data) != _HOST_KEYS or type(data["format"]) is not int or data["format"] != 1:
        _fail("history-request-invalid")
    if event.stream_id != f"session:{data['session_id']}":
        _fail()
    request = _request(data)
    prefix = events[: event.seq - 1]
    policy = _policy_for(prefix, request)
    _user_source(
        prefix,
        session_id=data["session_id"],
        turn_id=data["turn_id"],
        step_id=data["step_id"],
        user_message_ref=data["user_message_ref"],
        request=request,
        policy=policy,
    )
    recorded = [
        item
        for item in events[: event.seq]
        if item.type == "history/requested" and item.data.get("turn_id") == data["turn_id"]
    ]
    if len(recorded) > policy.max_requests:
        _fail("history-request-limit")
    return request


def _validate_receipt(
    events: tuple[EventEnvelope, ...], event: EventEnvelope
) -> HistoryPageRequest:
    from traceh.session.reference_requests import RECEIPT_FORMAT, TARGET_RULE

    data = event.data["data"]["history_receipt"]
    if (
        type(data) is not dict
        or set(data) != _RECEIPT_KEYS
        or type(data["format"]) is not int
        or data["format"] != RECEIPT_FORMAT
        or data["status"] != "accepted"
        or data["target_rule"] != TARGET_RULE
        or event.data.get("status") != "succeeded"
        or event.data.get("tool_name") != HISTORY_TOOL_NAME
        or event.data.get("tool_call_id") != data["tool_call_id"]
        or event.data.get("step_id") != data["source_step_id"]
        or event.stream_id != f"session:{data['session_id']}"
    ):
        _fail("history-receipt-invalid")
    request = _request(data)
    prefix = events[: event.seq - 1]
    policy = _policy_for(prefix, request)
    call = _model_source(
        prefix,
        session_id=data["session_id"],
        turn_id=data["turn_id"],
        step_id=data["source_step_id"],
        tool_call_id=data["tool_call_id"],
        request=request,
        policy=policy,
    )
    if call.seq >= event.seq:
        _fail()
    _model_page(prefix, call, request, policy)
    return request


def validate_history_request_events(events: tuple[EventEnvelope, ...]) -> None:
    """Validate accepted evidence even when no target Context was ever written."""
    if not events:
        return
    _prefix(events, events[0].stream_id.removeprefix("session:"))
    try:
        for event in events:
            if event.type == "history/requested":
                _validate_host_event(events, event)
            elif (
                event.type == "tool/result"
                and isinstance(event.data.get("data"), dict)
                and "history_receipt" in event.data["data"]
            ):
                _validate_receipt(events, event)
    except (IndexError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, HistoryRequestError):
            raise
        raise HistoryRequestError(getattr(error, "code", "history-request-invalid")) from error


def eligible_history_requests(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    policy: HistoryReadPolicy,
):
    _prefix(events, session_id)
    validate_history_request_events(events)
    return _collect_history_requests(
        events, session_id=session_id, turn_id=turn_id, step_id=step_id, policy=policy
    )


def matches_block(eligible: EligibleHistoryRequest, block: dict) -> bool:
    request = eligible.request
    return (
        block["kind"] == "history"
        and block["id"] == request.block_id
        and block["tier"] == request.requested_tier
        and block["provenance"]["page"] is not None
        and block["provenance"]["page"]["index"] == request.cursor.index
        and block["provenance"]["request_ref"] == eligible.request_ref
    )


def _collect_history_requests(events, *, session_id, turn_id, step_id, policy):
    from traceh.session.reference_requests import collect_requests

    return collect_requests(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        kind="history",
        policy=policy,
        immediate=lambda prefix, target: _immediate_history_requests(
            prefix, session_id=session_id, turn_id=turn_id, step_id=target, policy=policy
        ),
        matches=matches_block,
        identity=lambda eligible: canonical_json(eligible.request.cursor.to_dict()),
        body_tiers=frozenset({"section", "chunk"}),
    )


def _immediate_history_requests(
    events, *, session_id, turn_id, step_id, policy
) -> tuple[EligibleHistoryRequest, ...]:
    """Select from previously validated accepted events; no recursive source validation."""
    step, starts = _step(events, turn_id, step_id)
    if any(event.type == "runtime/cancel-requested" for event in events[starts[0].seq :]):
        return ()
    candidates: list[tuple[EventEnvelope, HistoryPageRequest]] = []
    if len(starts) == 1:
        for event in events[step.seq :]:
            if event.type == "history/requested":
                request = _request(event.data)
                if event.data["turn_id"] != turn_id or event.data["step_id"] != step_id:
                    _fail()
                candidates.append((event, request))
    else:
        previous = starts[-2]
        previous_id = previous.data["step_id"]
        endings = [
            event
            for event in events[previous.seq : step.seq - 1]
            if event.type == "step/end" and event.data.get("step_id") == previous_id
        ]
        if len(endings) != 1 or endings[0].data.get("reason") != "model_response":
            return ()
        for event in events[previous.seq : endings[0].seq - 1]:
            if (
                event.type == "tool/result"
                and isinstance(event.data.get("data"), dict)
                and "history_receipt" in event.data["data"]
            ):
                request = _request(event.data["data"]["history_receipt"])
                receipt = event.data["data"]["history_receipt"]
                if receipt["turn_id"] != turn_id or receipt["source_step_id"] != previous_id:
                    _fail()
                if event.data.get("error_type") != "RecoveredAfterCrash":
                    candidates.append((event, request))
    if len(candidates) > policy.max_requests:
        _fail("history-request-limit")
    seen: set[str] = set()
    eligible: list[EligibleHistoryRequest] = []
    for event, request in candidates:
        if request.cursor.policy_digest != policy.digest:
            _fail("history-request-policy-mismatch")
        key = canonical_json(request.cursor.to_dict())
        if key not in seen:
            seen.add(key)
            eligible.append(EligibleHistoryRequest(request, event_ref(event)))
    return tuple(eligible)
