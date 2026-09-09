"""Canonical head references shared by reference sources and derived indexes."""

from traceh.api.json_types import fingerprint


def stream_head(stream_id, events):
    event = events[-1] if events else None
    return {
        "stream_id": stream_id,
        "head_seq": len(events),
        "head_event_id": str(event.event_id) if event else None,
        "head_digest": fingerprint(event.to_dict()) if event else None,
    }


def validate_stream_head(value):
    if (
        type(value) is not dict
        or set(value) != set(stream_head("", ()))
        or type(value["stream_id"]) is not str
        or not value["stream_id"]
        or type(value["head_seq"]) is not int
        or value["head_seq"] < 0
    ):
        raise ValueError("context-source-head-invalid")
    if value["head_seq"] == 0:
        if value != stream_head(value["stream_id"], ()):
            raise ValueError("context-source-head-invalid")
    elif (
        type(value["head_event_id"]) is not str
        or not value["head_event_id"]
        or type(value["head_digest"]) is not str
        or len(value["head_digest"]) != 64
        or any(c not in "0123456789abcdef" for c in value["head_digest"])
    ):
        raise ValueError("context-source-head-invalid")
