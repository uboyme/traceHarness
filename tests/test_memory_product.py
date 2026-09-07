"""Real Product host and managed worktree inheritance, without L2 or real providers."""

import asyncio

import pytest
from memory_fixtures import approve, bind, closed_source, declare, memory_policy
from promotion_fixtures import (
    build_source_repository,
    capture_limits,
    make_bare_target,
    promotion_targets,
)
from test_product_f3_e2e import (
    _chat_runtime,
    _ChatProvider,
    _Console,
    _host_profile,
    _ProductProvider,
)
from test_workspace_supervision import _Factory, _Policy, _repository

from traceh.api.agents import AgentSpec
from traceh.api.memory import ProjectScopeLimits
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.cas import LocalArtifactCas
from traceh.cli.chat import run_chat
from traceh.cli.product import LineProductAdapter
from traceh.memory.service import MemoryService
from traceh.memory.sources import session_source
from traceh.product.chat import ProductTurnActions
from traceh.product.errors import ProductInputError
from traceh.product.host import build_product_chat_host
from traceh.projects.service import ProjectScopeService
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import ProcessAgentSupervisor
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
)
from traceh.workspaces.catalog import WorkspaceCatalogReader
from traceh.workspaces.errors import WorkspaceError


@pytest.mark.parametrize("mismatch", ["session", "store", "resolver"])
async def test_product_host_rejects_foreign_scope_owners_before_effects(tmp_path, mismatch):
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    runtime = _chat_runtime(tmp_path, store, actions)
    provider = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "managed",
        sources={"product-source": source},
    )
    other_provider = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "managed",
        sources={"product-source": source},
    )
    owner = runtime.sessions
    if mismatch == "session":
        owner = SessionService(runtime.sessions.store)
    elif mismatch == "store":
        owner = SessionService(InMemoryEventStore())
    scope = ProjectScopeService(
        owner,
        other_provider if mismatch == "resolver" else provider,
        ProjectScopeLimits(120, 100),
    )
    try:
        with pytest.raises(ProductInputError) as error:
            await build_product_chat_host(
                store=runtime.sessions.store,
                sessions=runtime.sessions,
                data_dir=tmp_path / "data",
                host_profile=_host_profile(RequestedTaskMode.SINGLE),
                providers={"product-provider": _ProductProvider()},
                workspace_provider=provider,
                artifact_cas=LocalArtifactCas(tmp_path / "cas"),
                promotion_targets=promotion_targets("product-target", target),
                capture_limits=capture_limits(),
                approver_id="operator",
                max_report_chars=4096,
                event_feed=runtime.events,
                project_scope=scope,
            )
        assert error.value.code == "product-project-owner-mismatch"
        assert await store.list_streams() == ()
        assert not provider.managed_root.exists()
    finally:
        await runtime.dispose()


async def test_real_product_worktrees_inherit_before_first_model_dispatch(tmp_path):
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    runtime = _chat_runtime(tmp_path, store, actions, _ChatProvider())
    await runtime.create_session(source, session_id="requester")
    provider = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "managed", sources={"product-source": source}
    )
    scope = ProjectScopeService(runtime.sessions, provider, ProjectScopeLimits(120, 100))
    await bind(scope, "requester", source_id="product-source")
    authority = MemoryService(scope, memory_policy())
    proposal = await declare(authority)
    await approve(authority, proposal)

    class CheckingProvider(_ProductProvider):
        def __init__(self):
            super().__init__()
            self.checked = 0

        async def complete(self, request):
            # Observe durable association at the actual Provider call, not after creation.
            workspaces = await WorkspaceCatalogReader(store).load()
            assert workspaces.workspaces
            for record in workspaces.workspaces:
                if record.session_id is not None:
                    assert (await authority.read(record.session_id)).active[
                        0
                    ].memory_id == "memory-goal"
                    self.checked += 1
            return await super().complete(request)

    product_provider = CheckingProvider()
    host = await build_product_chat_host(
        store=runtime.sessions.store,
        sessions=runtime.sessions,
        data_dir=tmp_path / "product-data",
        host_profile=_host_profile(RequestedTaskMode.SINGLE),
        providers={"product-provider": product_provider},
        workspace_provider=provider,
        artifact_cas=LocalArtifactCas(tmp_path / "cas"),
        promotion_targets=promotion_targets("product-target", target),
        capture_limits=capture_limits(),
        approver_id="local-human",
        max_report_chars=4096,
        event_feed=runtime.events,
        actions=actions,
        project_scope=scope,
    )
    console = _Console(("please add the accepted file", "yes, do it", "START"))
    assert (
        await run_chat(
            runtime,
            console.console,
            session_id="requester",
            timeline=False,
            product=LineProductAdapter(host, data_dir=tmp_path / "product-data"),
        )
        == 0
    )
    assert "awaiting_approval" in console.output, console.output
    assert product_provider.checked > 0
    catalog = await scope.catalog()
    assert len(catalog.sessions) >= 3
    assert all(
        e.data["inheritance"] is not None for s, e in catalog.sessions.items() if s != "requester"
    )
    records = (await WorkspaceCatalogReader(store).load()).workspaces
    root = next(record for record in records if record.owner_agent_id is None)
    leaf = await closed_source(
        runtime.sessions,
        root.session_id,
        body="The ownership milestone is complete.",
    )
    evidence = await session_source(scope, root.session_id, [str(leaf.event_id)], authority.policy)
    proposed = await authority.propose(
        "requester",
        proposal_id="milestone",
        body="The ownership milestone is complete.",
        sources=[evidence],
        operation_id="milestone-proposed",
        actor_id="operator",
        expected_head=2,
    )
    await approve(authority, proposed, memory_id="milestone-memory", fact_slot="milestone")
    await WorkspaceService(scope.store, provider).release(root.workspace_id)
    assert len((await authority.read("requester")).active) == 2
    with pytest.raises(WorkspaceError):
        await authority.read(root.session_id)


@pytest.mark.parametrize("cancel", [False, True])
async def test_project_binding_failure_converges_created_agent_before_return(tmp_path, cancel):
    source = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    store = InMemoryEventStore()
    provider = LocalGitWorkspaceProvider(managed_root=managed, sources={"trusted-source": source})
    workspaces = WorkspaceService(store, provider)
    factory = _Factory(store, managed)
    inner = ProcessAgentSupervisor(store=store, factory=factory)

    class GateBinding:
        def __init__(self):
            self.store = store
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def bind_agent(self, agent_id):
            # Prove the real Directory, Session and attached worktree already exist.
            handle = await workspaces.resolve_for_agent(agent_id)
            assert handle.session_id is not None
            self.entered.set()
            await self.release.wait()
            raise ValueError("injected-project-binding-failure")

    binding = GateBinding()
    supervisor = WorkspaceManagedAgentSupervisor(
        inner, workspaces, workspace_policy=_Policy(), project_binding=binding
    )
    call = asyncio.create_task(
        supervisor.create(AgentSpec("coder", "explicit-intent"), request_id="request")
    )
    try:
        await binding.entered.wait()
        assert len(factory.executions) == 1
        if cancel:
            call.cancel()
            call.cancel()
        binding.release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await call
        assert factory.executions[0].dispose_calls == 1
        assert await store.head("projects:catalog") == 0
        assert len((await WorkspaceCatalogReader(store).load()).workspaces) == 1
    finally:
        await supervisor.aclose()
