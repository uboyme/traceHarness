"""Derive History freshness from host Tool envelopes and the frozen current observation."""

from traceh.projects.events import referenced
from traceh.workspaces.observation import validate_observation


def observe_history(block, history, events, current):
    validate_observation(current)
    if current is None:
        return None, "unknown"
    refs = (
        history.leaf_refs(block["id"])
        if block["provenance"]["page"] is None
        else block["provenance"]["page"]["leaf_refs"]
    )
    tools = [referenced(events, ref) for ref in refs if ref["type"] == "tool/result"]
    observations = [event.data.get("workspace_observation") for event in tools]
    for value in observations:
        validate_observation(value)
    known = [
        value
        for value in observations
        if value is not None and value["source_revision"] is not None
    ]
    mismatches = [
        value
        for value in known
        if value["source_identity"] != current["source_identity"]
        or (
            current["source_revision"] is not None
            and value["source_revision"] != current["source_revision"]
        )
    ]
    if mismatches:
        source, freshness = mismatches[0], "stale"
    elif tools and len(known) == len(tools) and current["source_revision"] is not None:
        source, freshness = known[0], "matched"
    else:
        source, freshness = None, "unknown"
    return {
        "source_identity": source["source_identity"] if source else None,
        "source_revision": source["source_revision"] if source else None,
        "current_identity": current["source_identity"],
        "current_revision": current["source_revision"],
    }, freshness
