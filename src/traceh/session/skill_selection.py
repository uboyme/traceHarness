"""Host-owned append-only Skill selection; never a Plugin lifecycle authority."""

from __future__ import annotations

import asyncio

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import PendingEvent
from traceh.api.json_types import canonical_json, fingerprint
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability

SELECTION_SET = "skill/selection-set"
_KEYS = {
    "format",
    "session_id",
    "operation_id",
    "expected_head",
    "actor_id",
    "catalog_digest",
    "skills",
}


def head_ref(session_id, events):
    event = events[-1] if events else None
    return {
        "stream_id": f"context-selection:{session_id}",
        "head_seq": len(events),
        "head_event_id": str(event.event_id) if event else None,
        "head_digest": fingerprint(event.to_dict()) if event else None,
    }


def validate_head(data, session_id):
    if (
        type(data) is not dict
        or set(data) != set(head_ref(session_id, ()))
        or data["stream_id"] != f"context-selection:{session_id}"
        or type(data["head_seq"]) is not int
        or data["head_seq"] < 0
    ):
        raise ValueError("skill-selection-head-invalid")
    if data["head_seq"] == 0:
        if data != head_ref(session_id, ()):
            raise ValueError("skill-selection-head-invalid")
    elif (
        type(data["head_event_id"]) is not str
        or not data["head_event_id"]
        or not _digest(data["head_digest"])
    ):
        raise ValueError("skill-selection-head-invalid")


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def parse_selection(data):
    if type(data) is not dict or set(data) != _KEYS:
        raise ValueError("skill-selection-payload-invalid")
    if type(data["format"]) is not int or data["format"] != 1:
        raise ValueError("skill-selection-protocol-unsupported")
    for name in ("session_id", "operation_id", "actor_id"):
        if type(data[name]) is not str or not data[name]:
            raise ValueError("skill-selection-identity-invalid")
        data[name].encode("utf-8")
    if (
        type(data["expected_head"]) is not int
        or data["expected_head"] < 0
        or not _digest(data["catalog_digest"])
    ):
        raise ValueError("skill-selection-payload-invalid")
    skills = data["skills"]
    if type(skills) is not list:
        raise ValueError("skill-selection-payload-invalid")
    ids = []
    for item in skills:
        if type(item) is not dict or set(item) != {"skill_id", "version"}:
            raise ValueError("skill-selection-payload-invalid")
        if any(type(value) is not str or not value for value in item.values()):
            raise ValueError("skill-selection-payload-invalid")
        ids.append(item["skill_id"])
    if ids != sorted(set(ids)):
        raise ValueError("skill-selection-duplicate")
    canonical_json(data).encode("utf-8")
    return data


def project_selection(events, session_id):
    operations = set()
    for seq, event in enumerate(events, 1):
        data = parse_selection(event.data)
        if (
            event.stream_id != f"context-selection:{session_id}"
            or event.seq != seq
            or event.type != SELECTION_SET
            or data["session_id"] != session_id
            or data["expected_head"] != seq - 1
            or event.actor_id != data["actor_id"]
            or data["operation_id"] in operations
        ):
            raise ValueError("skill-selection-source-invalid")
        operations.add(data["operation_id"])
    return events[-1].data if events else None


def eligible_skills(selection, composition):
    if selection is None:
        return (), "not-selected"
    if selection["catalog_digest"] != composition.skill_catalog_digest:
        return (), "stale-selection"
    catalog = {item.skill_id: item for item in composition.skill_catalog}
    result = []
    for item in selection["skills"]:
        descriptor = catalog.get(item["skill_id"])
        if descriptor is None or descriptor.version != item["version"]:
            raise ValueError("skill-selection-catalog-mismatch")
        result.append(descriptor)
    return tuple(result), None if result else "not-selected"


class SkillSelectionWriteError(RuntimeError):
    def __init__(self, committed):
        self.committed = committed
        super().__init__("skill-selection-write-failed")


async def set_selection(
    sessions, session_id, composition, *, operation_id, expected_head, actor_id, skills
):
    """Called only by the host facade while holding its exact Composition lease."""
    await sessions.ensure_session(session_id)
    data = parse_selection(
        {
            "format": 1,
            "session_id": session_id,
            "operation_id": operation_id,
            "expected_head": expected_head,
            "actor_id": actor_id,
            "catalog_digest": composition.skill_catalog_digest,
            "skills": list(skills),
        }
    )
    # Detach caller-owned input before the first await in the write transaction.
    import json

    data = json.loads(canonical_json(data))
    eligible_skills(data, composition)
    stream = f"context-selection:{session_id}"
    async with sessions._lock(stream):
        events = await sessions.store.read(stream)
        project_selection(events, session_id)
        previous = next((e for e in events if e.data["operation_id"] == operation_id), None)
        if previous is not None:
            if canonical_json(previous.data) != canonical_json(data):
                raise ValueError("skill-selection-operation-conflict")
            return previous
        if len(events) != expected_head:
            raise ConcurrencyConflict("skill selection changed")
        task = asyncio.create_task(
            sessions.store.append(
                stream,
                expected_seq=expected_head,
                events=(PendingEvent(type=SELECTION_SET, data=data, actor_id=actor_id),),
                durability=Durability.SYNC,
            ),
            name="traceh-skill-selection-append",
        )
        try:
            return (await asyncio.shield(task))[0]
        except (asyncio.CancelledError, Exception) as primary:
            await await_worker_convergence(task)
            committed = await committed_after_failure(
                lambda: sessions.store.read(stream),
                lambda e: (
                    e.stream_id == stream
                    and e.seq == expected_head + 1
                    and e.type == SELECTION_SET
                    and e.actor_id == actor_id
                    and canonical_json(e.data) == canonical_json(data)
                ),
            )
            error = SkillSelectionWriteError(committed)
            if isinstance(primary, asyncio.CancelledError):
                raise primary from error
            raise error from primary
