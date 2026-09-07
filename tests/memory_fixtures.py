"""Explicit test-only authority limits and sources; never production defaults."""

from traceh.api.json_types import fingerprint
from traceh.api.memory import MemoryPolicy, ProjectMemoryConfig, ProjectScopeLimits
from traceh.memory.service import MemoryService
from traceh.projects.events import reference
from traceh.projects.service import ProjectScopeService
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


def memory_policy(**changes):
    return MemoryPolicy(
        **{
            "max_body_bytes": 1024,
            "max_sources": 4,
            "max_source_events": 400,
            "max_source_bytes": 400_000,
            "max_memory_events": 120,
            "denied_patterns": (r"fixture-private-input",),
            **changes,
        }
    )


class Resolver:
    def __init__(self, mappings):
        self.mappings = mappings

    async def project_fingerprint(self, source_id, workspace):
        path = self.mappings[source_id]
        if workspace is not None and workspace != path:
            raise ValueError("project-workspace-source-mismatch")
        return fingerprint({"source": str(path)})


def config(resolver):
    return ProjectMemoryConfig(ProjectScopeLimits(120, 100), memory_policy(), resolver)


async def setup(tmp_path, *, store=None, session_id="requester", project_id="project-orion"):
    store = store if store is not None else InMemoryEventStore()
    sessions = SessionService(store)
    root = tmp_path / "repository"
    root.mkdir(exist_ok=True)
    resolver = Resolver({"source-orion": root})
    scope = ProjectScopeService(sessions, resolver, ProjectScopeLimits(120, 100))
    await sessions.create_session(root, session_id=session_id)
    await bind(scope, session_id, project_id=project_id)
    return MemoryService(scope, memory_policy()), scope, sessions, store, resolver


async def bind(scope, session_id, *, project_id="project-orion", source_id="source-orion"):
    catalog = await scope.catalog()
    if project_id not in catalog.projects:
        await scope.create(
            project_id=project_id,
            label="Durable test project",
            operation_id=f"create:{fingerprint(project_id)}",
            actor_id="operator",
            expected_head=catalog.head,
        )
        await scope.bind_source(
            project_id=project_id,
            source_id=source_id,
            operation_id=f"source:{fingerprint(project_id)}",
            actor_id="operator",
            expected_head=(await scope.catalog()).head,
        )
    return await scope.bind_session(
        session_id,
        project_id=project_id,
        operation_id=f"bind:{fingerprint(session_id)}",
        actor_id="operator",
        expected_head=(await scope.catalog()).head,
    )


async def declare(
    authority,
    *,
    proposal_id="proposal-goal",
    body="Preserve reproducible decisions.",
    session_id="requester",
):
    view = await authority.read(session_id)
    return await authority.declare(
        session_id,
        proposal_id=proposal_id,
        body=body,
        statement=body,
        declaration_id=f"declaration:{proposal_id}",
        operation_id=f"propose:{proposal_id}",
        actor_id="operator",
        expected_head=view.head,
    )


async def approve(
    authority,
    proposal,
    *,
    memory_id="memory-goal",
    fact_slot="goal",
    session_id="requester",
    **changes,
):
    return await authority.approve(
        session_id,
        **{
            "proposal_ref": reference(proposal),
            "proposal_digest": proposal.data["proposal_digest"],
            "memory_id": memory_id,
            "fact_slot": fact_slot,
            "operation_id": f"approve:{memory_id}",
            "actor_id": "operator",
            "expected_head": (await authority.read(session_id)).head,
            **changes,
        },
    )


async def closed_source(
    sessions, session_id="requester", *, body="Preserve reproducible decisions."
):
    await sessions.append_session(session_id, "turn/start", {"turn_id": "evidence-turn"})
    leaf = await sessions.append_session(
        session_id,
        "user/message",
        {
            "turn_id": "evidence-turn",
            "message_id": "evidence-message",
            "content": body,
        },
    )
    await sessions.append_session(
        session_id, "turn/end", {"turn_id": "evidence-turn", "status": "completed"}
    )
    return leaf
