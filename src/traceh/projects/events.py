"""Exact project/control-event identities, references and convergent CAS append."""

from __future__ import annotations

import asyncio
import json

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.identity import is_agent_identifier
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import canonical_json, fingerprint
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict

PROJECT_STREAM = "projects:catalog"
BASE_KEYS = {"format", "operation_id", "actor_id", "expected_head", "project_id"}
REF_KEYS = {"stream_id", "event_id", "seq", "type", "digest"}


def identifier(value):
    if type(value) is not str or not is_agent_identifier(value):
        raise ValueError("context-authority-identity-invalid")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("context-authority-integer-invalid")
    return value


def digest(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("context-authority-digest-invalid")
    return value


def detached(value):
    # JSON materialization is also the boundary excluding executable Python values.
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("context-authority-payload-invalid")

    check(value)
    return json.loads(canonical_json(value))


def exact(value, keys):
    if type(value) is not dict or set(value) != keys:
        raise ValueError("context-authority-payload-invalid")
    return value


def reference(event):
    return {
        "stream_id": event.stream_id,
        "event_id": str(event.event_id),
        "seq": event.seq,
        "type": event.type,
        "digest": fingerprint(event.to_dict()),
    }


def parse_reference(value):
    exact(value, REF_KEYS)
    stream = value["stream_id"]
    if type(stream) is not str or ":" not in stream:
        raise ValueError("context-authority-stream-invalid")
    prefix, identity = stream.split(":", 1)
    identifier(prefix)
    identifier(identity)
    for key in ("event_id", "type"):
        identifier(value[key])
    integer(value["seq"], 1)
    digest(value["digest"])
    return value


def referenced(events, value):
    parse_reference(value)
    event = next((e for e in events if e.seq == value["seq"]), None)
    if event is None or reference(event) != value:
        raise ValueError("context-authority-source-mismatch")
    return event


def header(event, stream, seq, extra):
    if (
        type(event) is not EventEnvelope
        or event.stream_id != stream
        or type(event.seq) is not int
        or event.seq != seq
        or type(event.schema_version) is not int
        or event.schema_version != 1
    ):
        raise ValueError("context-authority-event-invalid")
    data = detached(event.data)
    exact(data, BASE_KEYS | extra)
    if type(data["format"]) is not int or data["format"] != 1:
        raise ValueError("context-authority-format-unsupported")
    for key in ("operation_id", "actor_id", "project_id"):
        identifier(data[key])
    if event.actor_id != data["actor_id"] or integer(data["expected_head"]) != seq - 1:
        raise ValueError("context-authority-event-binding-invalid")
    return data


def control_data(project_id, operation_id, actor_id, expected_head, **fields):
    return detached(
        {
            "format": 1,
            "project_id": identifier(project_id),
            "operation_id": identifier(operation_id),
            "actor_id": identifier(actor_id),
            "expected_head": integer(expected_head),
            **fields,
        }
    )


class AuthorityWriteError(RuntimeError):
    def __init__(self, committed):
        self.committed = committed
        super().__init__("context-authority-write-failed")


async def append_owned(store, stream, event_type, data, validate):
    """One control operation; the Store remains the only linearization/close owner."""
    data = detached(data)
    events = await store.read(stream)
    validate(events)
    for event in events:
        if event.data["operation_id"] == data["operation_id"]:
            if event.type != event_type or canonical_json(event.data) != canonical_json(data):
                raise ValueError("context-authority-operation-conflict")
            return event
    if len(events) != data["expected_head"]:
        raise ConcurrencyConflict("context authority head changed")
    pending = PendingEvent(type=event_type, data=data, actor_id=data["actor_id"])
    validate((*events, EventEnvelope.materialize(stream, len(events) + 1, pending)))
    task = asyncio.create_task(
        store.append(
            stream,
            expected_seq=len(events),
            events=(pending,),
        ),
        name="traceh-context-authority-append",
    )
    try:
        return (await asyncio.shield(task))[0]
    except (asyncio.CancelledError, Exception) as primary:
        await await_worker_convergence(task)
        matches = []

        def is_our_event(event):
            if (
                event.stream_id == stream
                and event.seq == len(events) + 1
                and event.type == event_type
                and event.actor_id == data["actor_id"]
                and canonical_json(event.data) == canonical_json(data)
            ):
                matches.append(event)
                return True
            return False

        committed = await committed_after_failure(
            lambda: store.read(stream),
            is_our_event,
        )
        if isinstance(primary, ConcurrencyConflict) and committed is True:
            return matches[0]
        failure = AuthorityWriteError(committed)
        if isinstance(primary, asyncio.CancelledError):
            raise primary from failure
        raise failure from primary
