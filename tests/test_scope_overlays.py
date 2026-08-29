"""D1 contracts for Application -> Workspace -> Preset -> Agent services."""

from __future__ import annotations

from pathlib import Path

import pytest
from plugin_fixtures import entry_point_for, provider_for

from traceh.api.plugins import PluginManifest
from traceh.api.services import ServiceKey
from traceh.kernel.registry import (
    ServiceApiMajorMismatchError,
    ServiceConflictError,
    ServiceOverrideRequiredError,
    ServiceRegistry,
)
from traceh.kernel.scope import Scope, ScopeChain, ScopedServiceBinding, ScopeKind
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError
from traceh.plugins.manager import PluginManager
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.prompt import PromptAssembler
from traceh.session.event_store import InMemoryEventStore
from traceh.tools.registry import ToolRegistry


@pytest.mark.parametrize("api_major", (0, -1, True, "1"))
def test_service_key_rejects_an_invalid_api_major(api_major: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ServiceKey("scope.contract", api_major)  # type: ignore[arg-type]


def test_service_key_rejects_a_blank_name() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        ServiceKey("   ", 1)


@pytest.mark.parametrize("replace", ("false", 1, None))
def test_scoped_service_binding_requires_a_real_boolean(replace: object) -> None:
    with pytest.raises(TypeError, match="replace must be a bool"):
        ScopedServiceBinding(
            ScopeKind.WORKSPACE,
            ServiceKey("scope.boolean", 1),
            "workspace",
            replace=replace,  # type: ignore[arg-type]
        )


def test_service_registry_requires_a_real_boolean_replace() -> None:
    key = ServiceKey[str]("scope.boolean", 1)
    registry = ServiceRegistry()
    registry.bind(key, "application")

    with pytest.raises(TypeError, match="replace must be a bool"):
        registry.bind(key, "workspace", replace="false")  # type: ignore[arg-type]

    assert registry.require(key) == "application"


def test_scope_chain_requires_explicit_nearer_override() -> None:
    key = ServiceKey[str]("scope.contract", 1)
    application = ServiceRegistry()
    application.bind(key, "application")

    with pytest.raises(ServiceOverrideRequiredError) as caught:
        ScopeChain.build(
            application,
            (ScopedServiceBinding(ScopeKind.WORKSPACE, key, "workspace"),),
        )

    assert caught.value.code == "service-override-requires-replace"
    assert caught.value.scope == "workspace"
    assert caught.value.existing_scope == "application"


def test_scope_chain_failure_does_not_pollute_the_application_registry() -> None:
    key = ServiceKey[str]("scope.transaction", 1)
    application = ServiceRegistry()

    with pytest.raises(ServiceOverrideRequiredError):
        ScopeChain.build(
            application,
            (
                ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
                ScopedServiceBinding(ScopeKind.WORKSPACE, key, "workspace"),
            ),
        )

    assert application.get(key) is None
    retry = ScopeChain.build(
        application,
        (
            ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
            ScopedServiceBinding(
                ScopeKind.WORKSPACE,
                key,
                "workspace",
                replace=True,
            ),
        ),
    )
    assert retry.effective.require(key) == "workspace"


def test_scope_chain_prefers_agent_and_reports_the_source_scope() -> None:
    key = ServiceKey[str]("scope.contract", 1)
    application = ServiceRegistry()
    # Deliberately pass the nearer binding first. ScopeChain must assemble by
    # hierarchy, not let caller ordering bypass the explicit override check.
    chain = ScopeChain.build(
        application,
        (
            ScopedServiceBinding(ScopeKind.AGENT, key, "agent", replace=True),
            ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
        ),
    )

    resolution = chain.effective.services.resolve(key)
    assert resolution is not None
    assert resolution.value == "agent"
    assert resolution.scope == "agent"
    assert chain.workspace.require(key) == "application"


def test_same_layer_duplicate_has_a_stable_diagnostic() -> None:
    key = ServiceKey[str]("scope.contract", 1)
    registry = ServiceRegistry(scope="preset")
    registry.bind(key, "first")

    with pytest.raises(ServiceConflictError) as caught:
        registry.bind(key, "second")

    assert caught.value.code == "service-already-bound"
    assert caught.value.scope == "preset"
    assert caught.value.existing_scope == "preset"


def test_explicit_override_must_keep_the_api_major() -> None:
    v1 = ServiceKey[str]("scope.contract", 1)
    v2 = ServiceKey[str]("scope.contract", 2)
    application = ServiceRegistry()
    application.bind(v1, "v1")
    workspace = ServiceRegistry(scope="workspace", parent=application)

    with pytest.raises(ServiceApiMajorMismatchError) as caught:
        workspace.bind(v2, "wrong-major", replace=True)

    assert caught.value.code == "service-override-api-major-mismatch"
    # A second major is a separate capability when it is not presented as an
    # override of v1.
    workspace.bind(v2, "v2")
    assert workspace.require(v1) == "v1"
    assert workspace.require(v2) == "v2"


async def test_reversing_an_override_reveals_the_ancestor_again() -> None:
    key = ServiceKey[str]("scope.contract", 1)
    application = ServiceRegistry()
    application.bind(key, "application")
    workspace = ServiceRegistry(scope="workspace", parent=application)

    registration = await workspace.provide(key, "workspace", replace=True)
    assert workspace.require(key) == "workspace"

    await registration.dispose()
    assert workspace.require(key) == "application"


def test_scope_hierarchy_cannot_skip_or_extend_layers() -> None:
    application = Scope(name="application")
    with pytest.raises(ValueError, match="scope hierarchy"):
        Scope(name="preset", parent=application, kind=ScopeKind.PRESET)

    workspace = Scope(name="workspace", parent=application)
    preset = Scope(name="preset", parent=workspace)
    agent = Scope(name="agent", parent=preset)
    with pytest.raises(ValueError, match="cannot have a child"):
        Scope(name="child", parent=agent)


async def test_default_runtime_and_step_lease_use_the_effective_agent_scope(
    tmp_path: Path,
) -> None:
    key = ServiceKey[str]("scope.contract", 1)
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        service_bindings=(
            ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
            ScopedServiceBinding(ScopeKind.WORKSPACE, key, "workspace", replace=True),
            ScopedServiceBinding(ScopeKind.AGENT, key, "agent", replace=True),
        ),
        event_store=InMemoryEventStore(),
    )
    try:
        assert runtime.scope is not None
        assert runtime.scope.kind is ScopeKind.AGENT
        assert runtime.services.require(key) == "agent"
        assert not hasattr(runtime.services, "provide")
        with pytest.raises(RuntimeError, match="read-only"):
            runtime.scope.provide(
                ServiceKey("scope.other", 1),
                "not-allowed",
            )

        async with runtime.loop.compositions.lease(
            workspace=tmp_path,
            session_id="scope-session",
            turn_id="scope-turn",
            step_id="scope-step",
        ) as active:
            assert active.scope is runtime.scope
            assert active.services is runtime.services
            assert active.services.require(key) == "agent"
    finally:
        await runtime.dispose()


async def test_replacement_publishes_a_new_scope_without_mutating_an_old_lease(
    tmp_path: Path,
) -> None:
    key = ServiceKey[str]("scope.contract", 1)
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        service_bindings=(
            ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
            ScopedServiceBinding(ScopeKind.PRESET, key, "preset", replace=True),
        ),
        event_store=InMemoryEventStore(),
    )
    try:
        lease = runtime.loop.compositions.lease(
            workspace=tmp_path,
            session_id="scope-session",
            turn_id="scope-turn",
            step_id="scope-step",
        )
        async with lease as old:
            old_services = old.services
            assert old_services is not None
            await runtime.replace_plugin_composition(())
            assert runtime.services is not old_services
            assert old_services.require(key) == "preset"
            assert runtime.services.require(key) == "preset"
    finally:
        await runtime.dispose()


async def test_application_plugin_cannot_read_a_nearer_workspace_override(
    tmp_path: Path,
) -> None:
    key = ServiceKey[str]("scope.contract", 1)
    seen: list[str] = []

    class ApplicationPlugin:
        manifest = PluginManifest("scope.observer", "1.0.0")

        async def setup(self, context, config) -> None:
            del config
            seen.append(context.require(key))

    plugin = ApplicationPlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        enabled_plugins=("scope.observer",),
        plugin_discovery=discovery,
        service_bindings=(
            ScopedServiceBinding(ScopeKind.APPLICATION, key, "application"),
            ScopedServiceBinding(ScopeKind.WORKSPACE, key, "workspace", replace=True),
        ),
        event_store=InMemoryEventStore(),
    )
    try:
        assert seen == ["application"]
        assert runtime.services.require(key) == "workspace"
    finally:
        await runtime.dispose()


async def test_two_runtime_agent_scopes_do_not_share_local_bindings(tmp_path: Path) -> None:
    key = ServiceKey[str]("scope.contract", 1)
    first = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "first"),
        service_bindings=(ScopedServiceBinding(ScopeKind.AGENT, key, "first-agent"),),
        event_store=InMemoryEventStore(),
    )
    second = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "second"),
        service_bindings=(ScopedServiceBinding(ScopeKind.AGENT, key, "second-agent"),),
        event_store=InMemoryEventStore(),
    )
    try:
        assert first.services.require(key) == "first-agent"
        assert second.services.require(key) == "second-agent"
        assert first.scope is not second.scope
    finally:
        await first.dispose()
        await second.dispose()


async def test_plugin_manager_prepare_preserves_existing_child_scope_bindings() -> None:
    key = ServiceKey[str]("scope.manager-blueprint", 1)
    services = ServiceRegistry()
    chain = ScopeChain.build(
        services,
        (ScopedServiceBinding(ScopeKind.AGENT, key, "agent-only"),),
    )
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=services,
        scope_chain=chain,
        discovery=PluginDiscovery(entry_points_provider=provider_for()),
    )

    candidate = await manager.prepare_activation_set(())
    try:
        assert chain.effective.require(key) == "agent-only"
        assert candidate.services.require(key) == "agent-only"
        resolution = candidate.services.resolve(key)
        assert resolution is not None
        assert resolution.scope == "agent"
    finally:
        await candidate.dispose()


async def test_plugin_application_service_cannot_create_an_implicit_late_override(
    tmp_path: Path,
) -> None:
    key = ServiceKey[str]("scope.late-contract", 1)

    class ServicePlugin:
        manifest = PluginManifest("scope.provider", "1.0.0")

        async def setup(self, context, config) -> None:
            del config
            await context.provide(key, "plugin-application")

    plugin = ServicePlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))

    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            enabled_plugins=("scope.provider",),
            plugin_discovery=discovery,
            # The application value does not exist until plugin publication.
            # Revalidation must still reject this implicit workspace shadow.
            service_bindings=(ScopedServiceBinding(ScopeKind.WORKSPACE, key, "workspace"),),
            event_store=InMemoryEventStore(),
        )

    assert caught.value.failures[0].code == "service-override-requires-replace"


async def test_explicit_workspace_override_can_target_a_plugin_service(
    tmp_path: Path,
) -> None:
    key = ServiceKey[str]("scope.late-contract", 1)

    class ServicePlugin:
        manifest = PluginManifest("scope.provider", "1.0.0")

        async def setup(self, context, config) -> None:
            del config
            await context.provide(key, "plugin-application")

    plugin = ServicePlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        enabled_plugins=("scope.provider",),
        plugin_discovery=discovery,
        service_bindings=(
            ScopedServiceBinding(
                ScopeKind.WORKSPACE,
                key,
                "workspace",
                replace=True,
            ),
        ),
        event_store=InMemoryEventStore(),
    )
    try:
        assert runtime.services.require(key) == "workspace"
        assert runtime.scope is not None
        assert runtime.scope.parent is not None
        assert runtime.scope.parent.parent is not None
        assert runtime.scope.parent.parent.require(key) == "workspace"
        assert runtime.scope.parent.parent.parent is not None
        assert runtime.scope.parent.parent.parent.require(key) == "plugin-application"
    finally:
        await runtime.dispose()


async def test_late_plugin_service_keeps_api_major_override_validation(
    tmp_path: Path,
) -> None:
    application_key = ServiceKey[str]("scope.late-contract", 1)
    workspace_key = ServiceKey[str]("scope.late-contract", 2)

    class ServicePlugin:
        manifest = PluginManifest("scope.provider", "1.0.0")

        async def setup(self, context, config) -> None:
            del config
            await context.provide(application_key, "plugin-application")

    plugin = ServicePlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))

    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            enabled_plugins=("scope.provider",),
            plugin_discovery=discovery,
            service_bindings=(
                ScopedServiceBinding(
                    ScopeKind.WORKSPACE,
                    workspace_key,
                    "workspace-v2",
                    replace=True,
                ),
            ),
            event_store=InMemoryEventStore(),
        )

    assert caught.value.failures[0].code == "service-override-api-major-mismatch"


async def test_plugin_application_api_major_conflict_keeps_plugin_attribution(
    tmp_path: Path,
) -> None:
    application_v1 = ServiceKey[str]("scope.plugin-major", 1)
    plugin_v2 = ServiceKey[str]("scope.plugin-major", 2)

    class ServicePlugin:
        manifest = PluginManifest("scope.major-provider", "1.0.0")

        async def setup(self, context, config) -> None:
            del config
            await context.provide(plugin_v2, "plugin-v2", replace=True)

    plugin = ServicePlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))

    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            enabled_plugins=("scope.major-provider",),
            plugin_discovery=discovery,
            service_bindings=(
                ScopedServiceBinding(
                    ScopeKind.APPLICATION,
                    application_v1,
                    "application-v1",
                ),
            ),
            event_store=InMemoryEventStore(),
        )

    assert len(caught.value.failures) == 1
    failure = caught.value.failures[0]
    assert failure.code == "service-override-api-major-mismatch"
    assert failure.plugin_id == "scope.major-provider"
