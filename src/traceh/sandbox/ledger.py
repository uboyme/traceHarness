"""Sandbox receipts in the caller's existing stream, with EventStore CAS admission."""

from dataclasses import asdict

from traceh.api.events import PendingEvent
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.sandbox import SandboxOwner
from traceh.session.event_store import ConcurrencyConflict, EventStore


class SandboxEventRecorder:
    def __init__(self, store: EventStore, stream_id: str, owner: SandboxOwner) -> None:
        self.store = store
        self.stream_id = stream_id
        self.owner = owner

    async def __call__(self, event_type: str, data: dict[str, JsonValue]) -> None:
        if event_type not in {"sandbox/request", "sandbox/outcome", "sandbox/publication"}:
            raise ValueError("sandbox-event-type-invalid")
        if data.get("owner") != asdict(self.owner):
            raise ValueError("sandbox-event-owner-mismatch")
        if data.get("digest") != fingerprint({k: v for k, v in data.items() if k != "digest"}):
            raise ValueError("sandbox-event-digest-mismatch")
        while True:
            events = await self.store.read(self.stream_id)
            related = [
                e
                for e in events
                if e.type.startswith("sandbox/")
                and e.data.get("execution_id") == data.get("execution_id")
            ]
            if event_type == "sandbox/request":
                if related:
                    raise ValueError("sandbox-execution-already-admitted")
                if self.owner.kind == "effect":
                    intents = [
                        e
                        for e in events
                        if e.type == "effect/intent"
                        and e.data.get("effect_id") == self.owner.owner_id
                    ]
                    if len(intents) != 1 or any(
                        intents[0].data.get(key) != getattr(self.owner, key)
                        for key in (
                            "session_id",
                            "turn_id",
                            "step_id",
                            "tool_call_id",
                            "agent_id",
                            "budget_admission",
                            "budget_reservation",
                        )
                    ):
                        raise ValueError("sandbox-effect-owner-mismatch")
            elif event_type == "sandbox/outcome":
                if len(related) != 1 or related[0].type != "sandbox/request":
                    raise ValueError("sandbox-outcome-without-open-request")
                request = related[0].data
                if request["owner"] != data["owner"] or request["digest"] != data.get(
                    "request_digest"
                ):
                    raise ValueError("sandbox-outcome-request-mismatch")
            else:
                if len(related) != 2 or related[-1].type != "sandbox/outcome":
                    raise ValueError("sandbox-publication-without-outcome")
                outcome = related[-1].data
                if outcome["owner"] != data["owner"] or outcome["digest"] != data.get(
                    "outcome_digest"
                ):
                    raise ValueError("sandbox-publication-outcome-mismatch")
                if outcome["status"] != "finished" or not outcome["converged"]:
                    raise ValueError("sandbox-publication-after-incomplete-execution")
            try:
                await self.store.append(
                    self.stream_id,
                    expected_seq=events[-1].seq if events else 0,
                    events=(PendingEvent(type=event_type, data=data),),
                )
                return
            except ConcurrencyConflict:
                continue
