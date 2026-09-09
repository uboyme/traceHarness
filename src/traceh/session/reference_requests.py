"""One event-derived disclosure lifetime shared by Skill, Memory and History."""

from collections.abc import Callable
from dataclasses import dataclass

from traceh.api.json_types import fingerprint
from traceh.session.history_requests import _step, event_ref

RECEIPT_FORMAT = 2
TARGET_RULE = "next-step-then-bounded-turn"


@dataclass(frozen=True, slots=True)
class RequestGroups[Request]:
    fresh: tuple[Request, ...]
    retained: tuple[Request, ...]

    @property
    def all(self) -> tuple[Request, ...]:
        return (*self.fresh, *self.retained)


def collect_requests[Request](
    events,
    *,
    session_id,
    turn_id,
    step_id,
    kind,
    policy,
    immediate: Callable[..., tuple[Request, ...]],
    matches: Callable[[Request, dict], bool],
    identity: Callable[[Request], str],
    body_tiers: frozenset[str],
) -> RequestGroups[Request]:
    """Replay contiguous admissions, retaining handles rather than body bytes.

    A failed first admission or any later eviction breaks the chain permanently.
    The returned view is bounded by one Step's fresh requests and the preceding
    Context's admitted blocks; it is never stored as a second authority.
    """
    from traceh.session.context_input import parse_context_input

    _, starts = _step(events, turn_id, step_id)
    if any(e.type == "runtime/cancel-requested" for e in events[starts[0].seq :]):
        return RequestGroups((), ())
    retained: tuple[Request, ...] = ()
    for index, target in enumerate(starts):
        if index:
            previous = starts[index - 1]
            ends = [e for e in events[previous.seq : target.seq - 1] if e.type == "step/end"]
            summary_step = (
                len(ends) == 1
                and ends[0].data.get("reason") == "semantic_summary"
                and any(
                    e.type == "summary/input" and e.data.get("step_id") == previous.data["step_id"]
                    for e in events[previous.seq : ends[0].seq - 1]
                )
            )
            if len(ends) != 1 or (
                ends[0].data.get("reason") != "model_response" and not summary_step
            ):
                return RequestGroups((), ())
        current = index == len(starts) - 1
        data = None
        if current:
            prefix = events
        else:
            contexts = [
                e
                for e in events[target.seq : starts[index + 1].seq - 1]
                if e.type == "context/input"
            ]
            if len(contexts) != 1:
                retained = ()
                continue
            event = contexts[0]
            data = parse_context_input(event.data).to_dict()
            if (
                data["session_id"] != session_id
                or data["turn_id"] != turn_id
                or data["step_id"] != target.data["step_id"]
                or not target.seq <= data["observed_session_seq"] < event.seq
            ):
                raise ValueError(f"{kind}-retention-binding-mismatch")
            # A policy transition never transfers a previous disclosure grant.
            if data["policy"]["config"]["skills" if kind == "skill" else kind] != policy.to_dict():
                retained = ()
                continue
            prefix = events[: data["observed_session_seq"]]
        fresh = immediate(prefix, target.data["step_id"])
        seen = {identity(request) for request in fresh}
        retained = tuple(request for request in retained if identity(request) not in seen)
        groups = RequestGroups(fresh, retained)
        if current:
            return groups
        assert data is not None
        # Preserve actual admission order, not an unbounded list of past requests.
        carried, seen = [], set()
        for block in data["blocks"]:
            if block["kind"] != kind or block["tier"] not in body_tiers:
                continue
            for request in groups.all:
                key = identity(request)
                if key not in seen and matches(request, block):
                    carried.append(request)
                    seen.add(key)
                    break
        retained = tuple(carried)
    return RequestGroups((), ())


def eligible_requests(
    events,
    *,
    session_id,
    turn_id,
    step_id,
    policy,
    kind,
    tool_name,
    parse_request,
    source_for_request,
    matches,
):
    return collect_requests(
        events,
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        kind=kind,
        policy=policy,
        immediate=lambda prefix, target: _immediate_requests(
            prefix,
            session_id=session_id,
            turn_id=turn_id,
            step_id=target,
            policy=policy,
            kind=kind,
            tool_name=tool_name,
            parse_request=parse_request,
            source_for_request=source_for_request,
        ),
        matches=matches,
        identity=fingerprint,
        body_tiers=frozenset({"section", "chunk"} if kind == "skill" else {"summary", "section"}),
    )


def _immediate_requests(
    events,
    *,
    session_id,
    turn_id,
    step_id,
    policy,
    kind,
    tool_name,
    parse_request,
    source_for_request,
):
    step, starts = _step(events, turn_id, step_id)
    if len(starts) < 2 or any(
        e.type == "runtime/cancel-requested" for e in events[starts[0].seq :]
    ):
        return ()
    previous = starts[-2]
    ends = [e for e in events[previous.seq : step.seq - 1] if e.type == "step/end"]
    if len(ends) != 1 or ends[0].data.get("reason") != "model_response":
        return ()
    result, seen = [], set()
    for event in events[previous.seq : ends[0].seq - 1]:
        if event.type != "tool/result" or event.data.get("error_type") == "RecoveredAfterCrash":
            continue
        data = event.data.get("data")
        if not isinstance(data, dict) or f"{kind}_receipt" not in data:
            continue
        if event.data.get("status") != "succeeded":
            raise ValueError(f"{kind}-receipt-not-successful")
        receipt = data[f"{kind}_receipt"]
        if type(receipt) is not dict or set(receipt) != {
            "format",
            "session_id",
            "turn_id",
            "source_step_id",
            "tool_call_id",
            "context_ref",
            "request",
            "policy_digest",
            "target_rule",
        }:
            raise ValueError(f"{kind}-receipt-invalid")
        if (
            type(receipt["format"]) is not int
            or receipt["format"] != RECEIPT_FORMAT
            or receipt["session_id"] != session_id
            or receipt["turn_id"] != turn_id
            or receipt["source_step_id"] != previous.data["step_id"]
            or receipt["tool_call_id"] != event.data.get("tool_call_id")
            or event.data.get("tool_name") != tool_name
            or receipt["policy_digest"] != policy.digest
            or receipt["target_rule"] != TARGET_RULE
        ):
            raise ValueError(f"{kind}-receipt-binding-mismatch")
        request = parse_request(receipt["request"])
        _, source, _ = source_for_request(
            events[: event.seq - 1],
            session_id=session_id,
            turn_id=turn_id,
            step_id=previous.data["step_id"],
            tool_call_id=receipt["tool_call_id"],
            request=request,
            policy=policy,
        )
        if receipt["context_ref"] != event_ref(source):
            raise ValueError(f"{kind}-receipt-binding-mismatch")
        key = fingerprint(request)
        if key not in seen:
            seen.add(key)
            result.append(request)
    if len(result) > policy.max_requests:
        raise ValueError(f"{kind}-request-resource-limit")
    return tuple(result)
