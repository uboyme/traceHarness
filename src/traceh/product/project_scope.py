"""Product-owned bridge proving the task/requester/Agent/Workspace association."""

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.json_types import fingerprint
from traceh.api.product import PRODUCT_TASK_STREAM_PREFIX
from traceh.product.execution import product_task_owner_id
from traceh.product.projection import rebuild_product_task
from traceh.projects.events import reference
from traceh.supervision.execution import durable_log_identity
from traceh.workspaces.events import WORKSPACE_CATALOG_STREAM


class ProductProjectBinding:
    def __init__(self, scope, workspaces, *, actor_id, memory_config=None, retrieval_policy=None):
        if durable_log_identity(scope.store) is not durable_log_identity(workspaces.store):
            raise ValueError("project-product-store-mismatch")
        self.scope = scope
        self.workspaces = workspaces
        self.actor_id = actor_id
        self.store = scope.store
        self.memory_config = memory_config
        self.retrieval_policy = retrieval_policy

    async def bind_agent(self, agent_id):
        # Resolve through the existing resource owner; no path or activation cache here.
        workspace = await self.workspaces.resolve_for_agent(agent_id)
        directory = await AgentDirectoryReader(self.store).load()
        agent = directory.get(agent_id)
        if agent is None:
            raise ValueError("project-agent-missing")
        root = agent
        while root.owner_agent_id is not None:
            root = directory.get(root.owner_agent_id)
            if root is None:
                raise ValueError("project-task-owner-missing")
        streams = await self.store.list_streams(prefix=PRODUCT_TASK_STREAM_PREFIX)
        if len(streams) > self.scope.limits.max_catalog_events:
            raise ValueError("project-task-catalog-limit")
        matches = []
        for stream in streams:
            task_id = stream[len(PRODUCT_TASK_STREAM_PREFIX) :]
            if product_task_owner_id(task_id) == root.agent_id:
                events = await self.store.read(stream)
                task = rebuild_product_task(task_id, events)
                if task is not None:
                    matches.append((task, events[0]))
        if len(matches) != 1:
            raise ValueError("project-task-owner-mismatch")
        task, opened = matches[0]
        catalog = await self.scope.catalog()
        requester = catalog.sessions.get(task.origin_session_id)
        if requester is None:
            # Explicitly unbound Product work remains Session-only. No default project.
            if agent.session_id in catalog.sessions:
                raise ValueError("project-requester-unbound")
            return
        requester = await self.scope.resolve(task.origin_session_id)
        existing = catalog.sessions.get(agent.session_id)
        if existing is not None:
            if existing.data["project_id"] != requester.data["project_id"]:
                raise ValueError("project-requester-mismatch")
            await self.scope.resolve(agent.session_id)
            await self._rebuild_index(agent.session_id)
            return
        agent_events = await self.store.read(AGENT_DIRECTORY_STREAM)
        workspace_events = await self.store.read(WORKSPACE_CATALOG_STREAM)
        attached = [
            e
            for e in workspace_events
            if e.type == "workspace/attached"
            and e.data.get("workspace_id") == workspace.workspace_id
        ]
        if len(attached) != 1:
            raise ValueError("project-workspace-owner-mismatch")
        inheritance = {
            "requester_binding_ref": reference(requester),
            "task_ref": reference(opened),
            "agent_ref": reference(agent_events[agent.created_seq - 1]),
            "workspace_ref": reference(attached[0]),
        }
        await self.scope.bind_session(
            agent.session_id,
            project_id=requester.data["project_id"],
            operation_id="project-inherit:" + fingerprint(inheritance),
            actor_id=self.actor_id,
            expected_head=catalog.head,
            inheritance=inheritance,
        )
        await self._rebuild_index(agent.session_id)

    async def _rebuild_index(self, session_id):
        """Host activation/send binding refreshes its Session-specific derived corpus."""
        if self.memory_config is None or self.retrieval_policy is None:
            return
        from traceh.memory.context import MemoryContextReader, prepare_corpus
        from traceh.memory.service import MemoryService

        source = await MemoryContextReader(
            MemoryService(self.scope, self.memory_config.memory_policy)
        ).read(session_id)
        if source is None:
            raise ValueError("project-session-unbound")
        corpus = prepare_corpus(source, self.retrieval_policy)[0]
        await self.scope.sessions.rebuild_context_index(corpus)


__all__ = ["ProductProjectBinding"]
