"""Bounded content and source shapes, shared by writers and replay."""

import re

from traceh.api.json_types import canonical_json
from traceh.projects.events import exact, identifier, integer, parse_reference

# Protocol/security shapes, never sample project names or model-dependent defaults.
DENIED = (
    r"(?i)(?:^|[/\\\s\"'])\.env(?:\b|\.)",
    r"(?i)-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password|secret|credential)s?\b[\"']?\s*[=:]",
    r"(?i)\b(?:authorization\s*:\s*bearer|sk-[a-z0-9_-]{16,})",
    r"(?i)\b(?:approval[_ -]?(?:token|capability)|evaluator[_ -]?input|budget[_ -]?credential)\b",
    r"(?:正在(?:修改|编辑|运行测试)|测试(?:正|目前)在运行|正在等待(?:审批|批准))",
    r"(?i)\b(?:currently (?:editing|running tests)|waiting for approval)\b",
)


def content(text, policy, *, limit):
    if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > limit:
        raise ValueError("memory-content-limit")
    if any(re.search(pattern, text) for pattern in (*DENIED, *policy.denied_patterns)):
        raise ValueError("memory-content-rejected")
    return text


def source_shapes(sources, policy):
    if type(sources) is not list or not 1 <= len(sources) <= policy.max_sources:
        raise ValueError("memory-source-count-invalid")
    if len(canonical_json(sources).encode("utf-8")) > policy.max_source_bytes:
        raise ValueError("memory-source-limit")
    count = 0
    for source in sources:
        if type(source) is not dict:
            raise ValueError("memory-source-invalid")
        kind = source.get("kind")
        if kind == "host-declaration":
            exact(source, {"kind", "declaration_id", "actor_id", "statement"})
            identifier(source["declaration_id"])
            identifier(source["actor_id"])
            content(source["statement"], policy, limit=policy.max_body_bytes)
            continue
        if kind == "session-evidence":
            exact(source, {"kind", "binding_ref", "event_refs", "observed_head"})
            parse_reference(source["binding_ref"])
        elif kind == "product-evidence":
            exact(
                source, {"kind", "requester_binding_ref", "task_id", "event_refs", "observed_head"}
            )
            parse_reference(source["requester_binding_ref"])
            identifier(source["task_id"])
        else:
            raise ValueError("memory-source-kind-invalid")
        head = integer(source["observed_head"], 1)
        refs = source["event_refs"]
        if type(refs) is not list or not refs:
            raise ValueError("memory-source-empty")
        identities = set()
        for ref in refs:
            parse_reference(ref)
            key = canonical_json(ref)
            if ref["seq"] > head or key in identities:
                raise ValueError("memory-source-ref-invalid")
            identities.add(key)
        count += len(refs)
    if count > policy.max_source_events:
        raise ValueError("memory-source-limit")
