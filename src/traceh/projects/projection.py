"""One pure replay of the host's immutable project ownership catalog."""

from dataclasses import dataclass

from traceh.api.events import EventEnvelope, detach_event
from traceh.api.json_types import fingerprint
from traceh.api.memory import ProjectScopeLimits
from traceh.projects.events import (
    PROJECT_STREAM,
    digest,
    exact,
    header,
    identifier,
    parse_reference,
    referenced,
)

FIELDS = {
    "project/created": {"label"},
    "project/source-bound": {"source_id", "repository_fingerprint", "source_binding_digest"},
    "project/session-bound": {"session_ref", "source_binding_ref", "inheritance"},
}


@dataclass(frozen=True, slots=True)
class ProjectCatalog:
    # Each replay owns its containers; these are never retained as a service cache.
    events: tuple[EventEnvelope, ...]
    projects: dict[str, EventEnvelope]
    sources: dict[str, EventEnvelope]
    sessions: dict[str, EventEnvelope]

    @property
    def head(self):
        return len(self.events)


def replay_projects(events, limits: ProjectScopeLimits) -> ProjectCatalog:
    if len(events) > limits.max_catalog_events:
        raise ValueError("project-catalog-limit")
    projects, sources, sessions = {}, {}, {}
    operations, source_ids, event_ids = set(), set(), set()
    frozen = []
    for seq, raw in enumerate(events, 1):
        if raw.type not in FIELDS:
            raise ValueError("project-event-unknown")
        data = header(raw, PROJECT_STREAM, seq, FIELDS[raw.type])
        event = detach_event(raw)
        operation, project = data["operation_id"], data["project_id"]
        if operation in operations or event.event_id in event_ids:
            raise ValueError("project-operation-duplicate")
        operations.add(operation)
        event_ids.add(event.event_id)
        if event.type == "project/created":
            label = data["label"]
            if (
                type(label) is not str
                or not label.strip()
                or len(label.encode("utf-8")) > limits.max_label_bytes
                or project in projects
            ):
                raise ValueError("project-create-invalid")
            projects[project] = event
        elif project not in projects:
            raise ValueError("project-not-created")
        elif event.type == "project/source-bound":
            identity = {
                "source_id": identifier(data["source_id"]),
                "repository_fingerprint": digest(data["repository_fingerprint"]),
            }
            key = fingerprint(identity)
            if data["source_binding_digest"] != key or project in sources or key in source_ids:
                raise ValueError("project-source-conflict")
            source_ids.add(key)
            sources[project] = event
        else:
            source = referenced(frozen, data["source_binding_ref"])
            if sources.get(project) != source:
                raise ValueError("project-source-binding-mismatch")
            ref = parse_reference(data["session_ref"])
            if ref["type"] != "session/created" or ref["seq"] != 1:
                raise ValueError("project-session-ref-invalid")
            if not ref["stream_id"].startswith("session:"):
                raise ValueError("project-session-ref-invalid")
            session = identifier(ref["stream_id"][len("session:") :])
            if session in sessions:
                raise ValueError("project-session-already-bound")
            inheritance = data["inheritance"]
            if inheritance is not None:
                exact(
                    inheritance, {"requester_binding_ref", "task_ref", "agent_ref", "workspace_ref"}
                )
                requester = referenced(frozen, inheritance["requester_binding_ref"])
                if requester.type != event.type or requester.data["project_id"] != project:
                    raise ValueError("project-inheritance-invalid")
                for key, kind in (
                    ("task_ref", "product/task-opened"),
                    ("agent_ref", "agent/created"),
                    ("workspace_ref", "workspace/attached"),
                ):
                    if parse_reference(inheritance[key])["type"] != kind:
                        raise ValueError("project-inheritance-invalid")
            sessions[session] = event
        frozen.append(event)
    return ProjectCatalog(tuple(frozen), projects, sources, sessions)
