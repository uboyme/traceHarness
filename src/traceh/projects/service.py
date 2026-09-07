"""Host project association; borrows Session/Store owners and owns no lifecycle."""

from pathlib import Path

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.memory import ProjectScopeLimits, ProjectSourceResolver
from traceh.api.workspaces import WorkspaceStatus
from traceh.product.events import product_task_stream
from traceh.product.execution import product_task_owner_id
from traceh.product.projection import rebuild_product_task
from traceh.projects.events import (
    PROJECT_STREAM,
    append_owned,
    control_data,
    detached,
    digest,
    identifier,
    reference,
    referenced,
)
from traceh.projects.projection import replay_projects
from traceh.session.service import SessionService
from traceh.workspaces.catalog import WorkspaceCatalogReader
from traceh.workspaces.events import WORKSPACE_CATALOG_STREAM


class ProjectScopeService:
    def __init__(
        self,
        sessions: SessionService,
        resolver: ProjectSourceResolver,
        limits: ProjectScopeLimits,
    ):
        if type(sessions) is not SessionService or type(limits) is not ProjectScopeLimits:
            raise ValueError("project-owner-or-limits-invalid")
        if not callable(getattr(resolver, "project_fingerprint", None)):
            raise ValueError("project-source-resolver-required")
        self.sessions = sessions
        self.store = sessions.store
        self.resolver = resolver
        self.limits = limits

    async def catalog(self):
        return replay_projects(await self.store.read(PROJECT_STREAM), self.limits)

    async def _append(self, kind, data):
        return await append_owned(
            self.store,
            PROJECT_STREAM,
            kind,
            data,
            lambda events: replay_projects(events, self.limits),
        )

    async def create(self, *, project_id, label, operation_id, actor_id, expected_head):
        data = control_data(project_id, operation_id, actor_id, expected_head, label=label)
        return await self._append("project/created", data)

    @staticmethod
    def _existing(catalog, kind, data, existing, fields):
        for event in catalog.events:
            if event.data["operation_id"] == data["operation_id"]:
                if event.type != kind or canonical_json(event.data) != canonical_json(data):
                    raise ValueError("context-authority-operation-conflict")
                return event
        if existing is not None:
            if any(existing.data[key] != data[key] for key in ("project_id", *fields)):
                raise ValueError("project-binding-conflict")
            return existing
        return None

    async def bind_source(self, *, project_id, source_id, operation_id, actor_id, expected_head):
        identifier(source_id)
        base = control_data(project_id, operation_id, actor_id, expected_head)
        value = digest(await self.resolver.project_fingerprint(source_id, None))
        data = {
            **base,
            "source_id": source_id,
            "repository_fingerprint": value,
            "source_binding_digest": fingerprint(
                {
                    "source_id": source_id,
                    "repository_fingerprint": value,
                }
            ),
        }
        catalog = await self.catalog()
        existing = self._existing(
            catalog,
            "project/source-bound",
            data,
            catalog.sources.get(project_id),
            ("source_id", "repository_fingerprint", "source_binding_digest"),
        )
        return existing or await self._append("project/source-bound", data)

    async def bind_session(
        self,
        session_id,
        *,
        project_id,
        operation_id,
        actor_id,
        expected_head,
        inheritance=None,
    ):
        # Freeze host refs before the first await. The model has no binding entry point.
        inheritance = detached(inheritance)
        base = control_data(project_id, operation_id, actor_id, expected_head)
        identifier(session_id)
        catalog = await self.catalog()
        source = catalog.sources.get(project_id)
        if source is None:
            raise ValueError("project-source-unbound")
        events = await self.sessions.read_session(session_id)
        if not events:
            raise ValueError("project-session-missing")
        data = {
            **base,
            "session_ref": reference(events[0]),
            "source_binding_ref": reference(source),
            "inheritance": inheritance,
        }
        # Use the same proof for initial binding and every subsequent read.
        await self._prove(catalog, data, set())
        existing = self._existing(
            catalog,
            "project/session-bound",
            data,
            catalog.sessions.get(session_id),
            ("session_ref", "source_binding_ref", "inheritance"),
        )
        return existing or await self._append("project/session-bound", data)

    async def resolve(self, session_id):
        return await self._resolve(session_id, current_workspace=True)

    async def resolve_evidence(self, session_id):
        """Prove historical membership without reviving a released workspace.

        The viewer still needs resolve(). Evidence uses the durable attachment
        and current source mapping; workspace cleanup does not erase history.
        """
        return await self._resolve(session_id, current_workspace=False)

    async def _resolve(self, session_id, *, current_workspace):
        identifier(session_id)
        catalog = await self.catalog()
        binding = catalog.sessions.get(session_id)
        if binding is None:
            raise ValueError("project-session-unbound")
        await self._prove(catalog, binding.data, set(), current_workspace=current_workspace)
        return binding

    async def _prove(self, catalog, data, seen, *, current_workspace=True):
        session_ref = data["session_ref"]
        session_id = session_ref["stream_id"][len("session:") :]
        if session_id in seen:
            raise ValueError("project-inheritance-cycle")
        seen.add(session_id)
        events = await self.sessions.read_session(session_id)
        created = referenced(events, session_ref)
        if (
            created.seq != 1
            or created.type != "session/created"
            or created.data.get("session_id") != session_id
        ):
            raise ValueError("project-session-mismatch")
        source = referenced(catalog.events, data["source_binding_ref"])
        if source != catalog.sources.get(data["project_id"]):
            raise ValueError("project-source-binding-mismatch")
        workspace = created.data.get("workspace")
        if type(workspace) is not str or not Path(workspace).is_absolute():
            raise ValueError("project-workspace-invalid")
        current = await self.resolver.project_fingerprint(
            source.data["source_id"],
            Path(workspace) if current_workspace else None,
        )
        if current != source.data["repository_fingerprint"]:
            raise ValueError("project-source-drift")
        directory = await AgentDirectoryReader(self.store).load()
        agent = directory.for_session(session_id)
        inheritance = data["inheritance"]
        if inheritance is None:
            if agent is not None:
                raise ValueError("project-managed-inheritance-required")
            return
        from traceh.projects.events import exact

        exact(inheritance, {"requester_binding_ref", "task_ref", "agent_ref", "workspace_ref"})
        requester = referenced(catalog.events, inheritance["requester_binding_ref"])
        if (
            requester.type != "project/session-bound"
            or requester.data["project_id"] != data["project_id"]
        ):
            raise ValueError("project-requester-mismatch")
        await self._prove(catalog, requester.data, seen, current_workspace=False)
        task_id = inheritance["task_ref"]["stream_id"].removeprefix("product-task:")
        task_events = await self.store.read(product_task_stream(task_id))
        task = rebuild_product_task(task_id, task_events)
        opened = referenced(task_events, inheritance["task_ref"])
        requester_id = requester.data["session_ref"]["stream_id"].removeprefix("session:")
        if (
            task is None
            or opened.type != "product/task-opened"
            or task.origin_session_id != requester_id
            or task.confirmation_session_id != requester_id
        ):
            raise ValueError("project-task-requester-mismatch")
        if agent is None:
            raise ValueError("project-agent-missing")
        agent_events = await self.store.read(AGENT_DIRECTORY_STREAM)
        agent_event = referenced(agent_events, inheritance["agent_ref"])
        if agent_event.seq != agent.created_seq:
            raise ValueError("project-agent-mismatch")
        root = agent
        while root.owner_agent_id is not None:
            root = directory.get(root.owner_agent_id)
            if root is None:
                raise ValueError("project-task-owner-missing")
        if root.agent_id != product_task_owner_id(task.task_id):
            raise ValueError("project-task-owner-mismatch")
        workspaces = await WorkspaceCatalogReader(self.store).load()
        record = workspaces.for_session(session_id)
        workspace_event = referenced(
            await self.store.read(WORKSPACE_CATALOG_STREAM),
            inheritance["workspace_ref"],
        )
        if (
            record is None
            or (current_workspace and record.status is not WorkspaceStatus.ATTACHED)
            or record.agent_id != agent.agent_id
            or record.workspace_id != agent.workspace_id
            or record.owner_agent_id != agent.owner_agent_id
            or record.creation_request_id != agent.request_id
            or record.source_id != source.data["source_id"]
            or record.repository_fingerprint != source.data["repository_fingerprint"]
            or workspace_event.type != "workspace/attached"
            or workspace_event.data.get("workspace_id") != record.workspace_id
            or workspace_event.data.get("agent_id") != agent.agent_id
            or workspace_event.data.get("session_id") != session_id
        ):
            raise ValueError("project-workspace-owner-mismatch")


__all__ = ["ProjectScopeService"]
