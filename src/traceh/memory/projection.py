"""The sole Memory state machine. All views come from replay, never active flags."""

from dataclasses import dataclass

from traceh.api.events import EventEnvelope, detach_event
from traceh.api.json_types import fingerprint
from traceh.memory.policy import content, source_shapes
from traceh.projects.events import digest, header, identifier, referenced

PROPOSAL_FIELDS = {"proposal_id", "body", "sources", "proposal_digest"}
APPROVAL_FIELDS = {"proposal_ref", "proposal_digest", "memory_id", "fact_slot", "decision_digest"}
PREDECESSOR_FIELDS = {"predecessor_ref", "predecessor_digest"}
FIELDS = {
    "memory/proposed": PROPOSAL_FIELDS,
    "memory/approved": APPROVAL_FIELDS,
    "memory/superseded": APPROVAL_FIELDS | PREDECESSOR_FIELDS,
    "memory/revoked": {"memory_id", "fact_slot", "decision_digest"} | PREDECESSOR_FIELDS,
}


def memory_stream(project_id):
    return f"memory:{identifier(project_id)}"


def proposal_digest(data):
    return fingerprint({key: data[key] for key in ("project_id", "proposal_id", "body", "sources")})


def decision_digest(data):
    return fingerprint({key: value for key, value in data.items() if key != "decision_digest"})


@dataclass(frozen=True, slots=True)
class MemoryFact:
    memory_id: str
    fact_slot: str
    body: str
    proposal: EventEnvelope
    activation: EventEnvelope


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    project_id: str
    head: int
    proposals: tuple[EventEnvelope, ...]
    active: tuple[MemoryFact, ...]
    events: tuple[EventEnvelope, ...]


def replay_memory(project_id, events, policy):
    stream = memory_stream(project_id)
    if len(events) > policy.max_memory_events:
        raise ValueError("memory-stream-limit")
    proposals, active, frozen = {}, {}, []
    operations, memory_ids, consumed, event_ids = set(), set(), set(), set()
    for seq, raw in enumerate(events, 1):
        if raw.type not in FIELDS:
            raise ValueError("memory-event-unknown")
        data = header(raw, stream, seq, FIELDS[raw.type])
        if data["project_id"] != project_id:
            raise ValueError("memory-project-mismatch")
        event = detach_event(raw)
        if data["operation_id"] in operations or event.event_id in event_ids:
            raise ValueError("memory-operation-duplicate")
        operations.add(data["operation_id"])
        event_ids.add(event.event_id)
        if event.type == "memory/proposed":
            proposal_id = identifier(data["proposal_id"])
            content(data["body"], policy, limit=policy.max_body_bytes)
            source_shapes(data["sources"], policy)
            if any(
                source["kind"] == "host-declaration" and source["actor_id"] != data["actor_id"]
                for source in data["sources"]
            ):
                raise ValueError("memory-declaration-actor-mismatch")
            if proposal_id in proposals or data["proposal_digest"] != proposal_digest(data):
                raise ValueError("memory-proposal-conflict")
            proposals[proposal_id] = event
        else:
            slot, memory_id = identifier(data["fact_slot"]), identifier(data["memory_id"])
            if digest(data["decision_digest"]) != decision_digest(data):
                raise ValueError("memory-decision-digest-mismatch")
            previous = active.get(slot)
            if event.type == "memory/approved":
                if previous is not None:
                    raise ValueError("memory-slot-occupied")
            else:
                predecessor = referenced(frozen, data["predecessor_ref"])
                if (
                    previous is None
                    or previous.activation != predecessor
                    or data["predecessor_digest"] != previous.proposal.data["proposal_digest"]
                ):
                    raise ValueError("memory-predecessor-mismatch")
                if event.type == "memory/revoked" and memory_id != previous.memory_id:
                    raise ValueError("memory-predecessor-mismatch")
            if event.type == "memory/revoked":
                del active[slot]
            else:
                proposal = referenced(frozen, data["proposal_ref"])
                if (
                    proposal.type != "memory/proposed"
                    or proposal.event_id in consumed
                    or data["proposal_digest"] != proposal.data["proposal_digest"]
                    or memory_id in memory_ids
                ):
                    raise ValueError("memory-approval-mismatch")
                consumed.add(proposal.event_id)
                memory_ids.add(memory_id)
                active[slot] = MemoryFact(memory_id, slot, proposal.data["body"], proposal, event)
        frozen.append(event)
    return MemorySnapshot(
        project_id,
        len(events),
        tuple(proposals.values()),
        tuple(active[key] for key in sorted(active)),
        tuple(frozen),
    )
