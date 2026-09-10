"""Read exact sandbox evidence from the original event stream and CAS."""

import json
from dataclasses import dataclass

from traceh.api.artifacts import ArtifactCas, PatchBlob
from traceh.api.json_types import JsonValue, fingerprint
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class SandboxExecutionView:
    request: dict[str, JsonValue]
    outcome: dict[str, JsonValue] | None
    result: dict[str, JsonValue] | None
    publication: dict[str, JsonValue] | None = None


async def read_execution_record(
    store: EventStore, *, stream_id: str, execution_id: str
) -> SandboxExecutionView:
    """Verify ledger links for observation, without reading command output or workspace bytes."""
    events = [
        e
        for e in await store.read(stream_id)
        if e.type.startswith("sandbox/") and e.data.get("execution_id") == execution_id
    ]
    if not events or events[0].type != "sandbox/request" or len(events) > 3:
        raise ValueError("sandbox-execution-evidence-invalid")
    for event in events:
        data = event.data
        if data.get("digest") != fingerprint({k: v for k, v in data.items() if k != "digest"}):
            raise ValueError("sandbox-execution-digest-mismatch")
    request = events[0].data
    if len(events) == 1:
        return SandboxExecutionView(request, None, None)
    outcome = events[1].data
    if events[1].type != "sandbox/outcome" or (
        outcome.get("request_digest") != request["digest"]
        or outcome.get("owner") != request["owner"]
        or outcome.get("policy_digest") != fingerprint(request["policy"])
    ):
        raise ValueError("sandbox-execution-request-mismatch")
    publication = None
    if len(events) == 3:
        publication = events[2].data
        if events[2].type != "sandbox/publication" or (
            publication.get("outcome_digest") != outcome["digest"]
            or publication.get("owner") != request["owner"]
            or outcome["status"] != "finished"
            or not outcome["converged"]
        ):
            raise ValueError("sandbox-execution-publication-mismatch")
    return SandboxExecutionView(request, outcome, None, publication)


async def read_execution(
    store: EventStore, cas: ArtifactCas, *, stream_id: str, execution_id: str
) -> SandboxExecutionView:
    record = await read_execution_record(store, stream_id=stream_id, execution_id=execution_id)
    # CAS read validates size and bytes against the immutable reference.
    await cas.read(PatchBlob(**record.request["input_blob"]))
    result = None
    if record.outcome is not None:
        result = json.loads(await cas.read(PatchBlob(**record.outcome["output_blob"])))
        if (result.get("status"), result.get("converged")) != (
            record.outcome.get("status"), record.outcome.get("converged"),
        ):
            raise ValueError("sandbox-execution-result-mismatch")
    return SandboxExecutionView(record.request, record.outcome, result, record.publication)
