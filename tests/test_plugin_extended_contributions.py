"""Generation-scoped Provider, Policy, Middleware and Verifier plugins."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from plugin_fixtures import (
    RecordingTool,
    ScriptedPlugin,
    entry_point_for,
    manifest,
    provider_for,
)

from traceh.api.llm import ModelRequest, ModelResponse, ToolCall
from traceh.api.services import ServiceKey
from traceh.api.tools import ToolOutput
from traceh.kernel import ScopedPolicyBinding, ScopeKind
from traceh.kernel.registry import ServiceRegistry
from traceh.llm.registry import LlmRegistry
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError, PluginDisposeError
from traceh.plugins.manager import (
    PluginActivationSet,
    PluginGenerationBuilder,
    PluginManager,
)
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.composition_runtime import CompositionGeneration
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.verification import VerificationResult
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.policy import DecisionKind, ToolDecision
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


def _discovery(*plugins: ScriptedPlugin) -> PluginDiscovery:
    return PluginDiscovery(
        entry_points_provider=provider_for(
            *(entry_point_for(plugin) for plugin in plugins)
        )
    )


class _Provider:
    def __init__(self, name: str, responses: tuple[ModelResponse, ...]) -> None:
        self.name = name
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        return self._responses.pop(0)


class _RecordingPolicy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def check(self, call, tool, context) -> ToolDecision:
        del call, tool, context
        self.calls += 1
        return ToolDecision(DecisionKind.DEFER, policy=self.name)


class _RecordingMiddleware:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def invoke(self, invocation, call_next) -> ToolOutput:
        del invocation
        self.calls += 1
        result = await call_next()
        return ToolOutput(
            content=f"{result.content} | middleware-observed",
            data=dict(result.data),
            evidence=result.evidence,
        )


class _Verifier:
    def __init__(
        self,
        summary: str,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.summary = summary
        self.entered = entered
        self.release = release
        self.calls = 0

    async def verify(self, workspace: Path) -> VerificationResult:
        del workspace
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return VerificationResult(True, self.summary, 0)


class _TransferAbort(BaseException):
    """Deterministic stand-in for a synchronous hand-off interruption."""


def _builder_with_drifted_host_tool(
    plugin: ScriptedPlugin,
) -> PluginGenerationBuilder:
    tools = ToolRegistry()
    host_tool = RecordingTool("host-transfer-tool")
    tools.register(host_tool)
    host_tool.name = "renamed-host-transfer-tool"
    return PluginGenerationBuilder(
        tools=tools,
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=LlmRegistry(),
        discovery=_discovery(plugin),
    )


async def test_transfer_receipt_failure_rolls_back_the_activated_manager() -> None:
    health_entered = asyncio.Event()
    health_release = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("transfer-receipt-failure.extension"),
        spawn_forever=True,
        has_health_check=True,
        health_result=True,
        health_entered=health_entered,
        health_gate=health_release,
    )
    builder = _builder_with_drifted_host_tool(plugin)
    preparing = asyncio.create_task(
        builder.prepare((plugin.manifest.plugin_id,)),
    )
    await health_entered.wait()
    await plugin.owned_task_started.wait()
    health_release.set()

    with pytest.raises(
        ValueError,
        match="capability identity changed before transfer",
    ):
        await preparing

    assert plugin.setup_calls == 1
    assert plugin.cleanup_calls == 1
    assert plugin.owned_task is not None
    assert plugin.owned_task.done()
    assert plugin.owned_task_cancelled is True


async def test_transfer_failure_cleanup_absorbs_repeated_cancellation() -> None:
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("transfer-cancellation.extension"),
        cleanup_entered=cleanup_entered,
        cleanup_gate=cleanup_release,
    )
    builder = _builder_with_drifted_host_tool(plugin)
    preparing = asyncio.create_task(
        builder.prepare((plugin.manifest.plugin_id,)),
    )
    await cleanup_entered.wait()

    for _ in range(3):
        preparing.cancel()
        await asyncio.sleep(0)
        assert not preparing.done()
    assert plugin.cleanup_calls == 1

    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert plugin.cleanup_calls == 1


async def test_transfer_failure_and_cleanup_failure_are_both_reported() -> None:
    plugin = ScriptedPlugin(
        manifest("transfer-cleanup-failure.extension"),
        cleanup_error=RuntimeError("FAKE-FIXTURE transfer cleanup failure"),
    )
    builder = _builder_with_drifted_host_tool(plugin)

    with pytest.raises(ExceptionGroup) as caught:
        await builder.prepare((plugin.manifest.plugin_id,))

    assert caught.value.message == "plugin activation transfer cleanup failed"
    assert len(caught.value.exceptions) == 2
    assert isinstance(caught.value.exceptions[0], ValueError)
    assert isinstance(caught.value.exceptions[1], PluginDisposeError)
    assert plugin.cleanup_calls == 1


async def test_base_exception_transfer_and_cleanup_failure_are_both_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = ScriptedPlugin(
        manifest("transfer-base-exception.extension"),
        cleanup_error=RuntimeError("FAKE-FIXTURE transfer cleanup failure"),
    )
    builder = PluginGenerationBuilder(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=LlmRegistry(),
        discovery=_discovery(plugin),
    )

    def abort_transfer(_manager: PluginManager) -> PluginActivationSet:
        raise _TransferAbort("FAKE-FIXTURE synchronous transfer interruption")

    monkeypatch.setattr(PluginManager, "_transfer_activation_set", abort_transfer)

    with pytest.raises(BaseExceptionGroup) as caught:
        await builder.prepare((plugin.manifest.plugin_id,))

    assert not isinstance(caught.value, ExceptionGroup)
    assert caught.value.message == "plugin activation transfer cleanup failed"
    assert len(caught.value.exceptions) == 2
    assert isinstance(caught.value.exceptions[0], _TransferAbort)
    assert isinstance(caught.value.exceptions[1], PluginDisposeError)
    assert plugin.cleanup_calls == 1


async def test_explicitly_selected_plugin_provider_runs_the_real_loop(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _Provider(
        "tenant-provider",
        (ModelResponse(content="provider contribution reached the loop"),),
    )
    plugin = ScriptedPlugin(
        manifest("provider.extension"),
        providers=(provider,),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider=provider.name,
            model="provider-model",
        ),
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=_discovery(plugin),
        event_store=InMemoryEventStore(),
    )
    try:
        result = await runtime.run(workspace, "use the selected provider")
        assert result.final_text == "provider contribution reached the loop"
        assert provider.calls == 1
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        starts = [event for event in events if event.type == "model/attempt-start"]
        assert [event.data["provider"] for event in starts] == [provider.name]
    finally:
        await runtime.dispose()


async def test_missing_selected_provider_fails_before_plugin_health(
    tmp_path: Path,
) -> None:
    plugin = ScriptedPlugin(
        manifest("unrelated.extension"),
        has_health_check=True,
        health_result=True,
    )
    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(
                data_dir=tmp_path / "data",
                provider="required-provider",
            ),
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(plugin),
            event_store=InMemoryEventStore(),
        )
    assert [failure.code for failure in caught.value.failures] == [
        "provider-not-provided"
    ]
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1


async def test_missing_selected_verifier_fails_before_plugin_health(
    tmp_path: Path,
) -> None:
    plugin = ScriptedPlugin(
        manifest("unrelated-verifier.extension"),
        has_health_check=True,
        health_result=True,
    )
    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            verifier_name="required-check",
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(plugin),
            event_store=InMemoryEventStore(),
        )
    assert [failure.code for failure in caught.value.failures] == [
        "verifier-not-provided"
    ]
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1


async def test_plugin_policy_and_middleware_execute_on_the_tool_mainline(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = RecordingTool("review_input", content="raw tool output")
    policy = _RecordingPolicy("audit-policy")
    middleware = _RecordingMiddleware("audit-middleware")
    plugin = ScriptedPlugin(
        manifest("execution.extension"),
        tools=(tool,),
        policies=(policy,),
        middlewares=(middleware,),
    )
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("call-1", tool.name, {}),),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done"),
        )
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=_discovery(plugin),
        event_store=InMemoryEventStore(),
    )
    try:
        await runtime.run(workspace, "exercise tool composition")
        assert policy.calls == 1
        assert middleware.calls == 1
        assert tool.calls == 1
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        result = next(event for event in events if event.type == "tool/result")
        assert "middleware-observed" in str(result.data)
        snapshots = [event for event in events if event.type == "composition/snapshot"]
        assert "audit-policy" in snapshots[0].data["policies"]
        assert "audit-middleware" in snapshots[0].data["tool_middlewares"]
    finally:
        await runtime.dispose()


async def test_named_plugin_verifier_is_explicit_and_runs_inside_the_step_lease(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = _Verifier("plugin verifier passed")
    plugin = ScriptedPlugin(
        manifest("verification.extension"),
        verifiers=(("workspace-check", verifier),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "selected"),
        verifier_name="workspace-check",
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=_discovery(plugin),
        event_store=InMemoryEventStore(),
    )
    try:
        result = await runtime.run(workspace, "verify completion")
        assert result.verification_passed is True
        assert verifier.calls == 1
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        verification = next(
            event for event in events if event.type == "verification/result"
        )
        assert verification.data["summary"] == "plugin verifier passed"
    finally:
        await runtime.dispose()

    unused = _Verifier("must not run")
    unselected_plugin = ScriptedPlugin(
        manifest("optional-verification.extension"),
        verifiers=(("optional-check", unused),),
    )
    unselected = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "unselected"),
        enabled_plugins=(unselected_plugin.manifest.plugin_id,),
        plugin_discovery=_discovery(unselected_plugin),
        event_store=InMemoryEventStore(),
    )
    try:
        await unselected.run(workspace, "no implicit verifier replacement")
        assert unused.calls == 0
    finally:
        await unselected.dispose()


async def test_old_step_keeps_its_verifier_while_a_new_generation_is_published(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entered = asyncio.Event()
    release = asyncio.Event()
    old_verifier = _Verifier("old", entered=entered, release=release)
    new_verifier = _Verifier("new")
    cleanup_entered = asyncio.Event()
    old_plugin = ScriptedPlugin(
        manifest("old-verification.extension"),
        verifiers=(("generation-check", old_verifier),),
        cleanup_entered=cleanup_entered,
    )
    new_plugin = ScriptedPlugin(
        manifest("new-verification.extension"),
        verifiers=(("generation-check", new_verifier),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        verifier_name="generation-check",
        enabled_plugins=(old_plugin.manifest.plugin_id,),
        plugin_discovery=_discovery(old_plugin),
        event_store=InMemoryEventStore(),
    )
    try:
        session_id = await runtime.create_session(workspace)
        running = asyncio.create_task(runtime.run_existing(session_id, "hold old lease"))
        await entered.wait()
        replacement = await runtime.replace_plugin_composition(
            (new_plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(new_plugin),
        )
        assert replacement.enabled_plugin_ids == (new_plugin.manifest.plugin_id,)
        assert old_plugin.cleanup_calls == 0
        assert new_verifier.calls == 0
        release.set()
        result = await running
        assert result.verification_passed is True
        assert old_verifier.calls == 1
        assert new_verifier.calls == 0
        await cleanup_entered.wait()
        assert old_plugin.cleanup_calls == 1
    finally:
        release.set()
        await runtime.dispose()


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    (
        ("policy", "policy-publish-conflict"),
        ("middleware", "middleware-publish-conflict"),
    ),
)
async def test_plugin_execution_conflicts_fail_before_health(
    tmp_path: Path,
    kind: str,
    expected_code: str,
) -> None:
    policy = _RecordingPolicy("shared-capability")
    middleware = _RecordingMiddleware("shared-capability")
    plugin = ScriptedPlugin(
        manifest(f"{kind}.extension"),
        policies=(policy,) if kind == "policy" else (),
        middlewares=(middleware,) if kind == "middleware" else (),
        has_health_check=True,
        health_result=True,
    )
    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / kind),
            policies=(policy,) if kind == "policy" else None,
            tool_middlewares=(middleware,) if kind == "middleware" else (),
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(plugin),
            event_store=InMemoryEventStore(),
        )
    assert [failure.code for failure in caught.value.failures] == [expected_code]
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1


@pytest.mark.parametrize("kind", ("policy", "middleware"))
async def test_health_check_cannot_add_late_execution_contributions(kind: str) -> None:
    host_policy = _RecordingPolicy("late-shared-capability")
    host_middleware = _RecordingMiddleware("late-shared-capability")

    class _LateContributionPlugin:
        manifest = manifest(f"late-{kind}.extension")

        def __init__(self) -> None:
            self.health_calls = 0

        async def setup(self, context, config) -> None:
            del context, config

        async def health_check(self, context) -> bool:
            self.health_calls += 1
            if kind == "policy":
                context.register_policy(_RecordingPolicy(host_policy.name))
            else:
                context.register_middleware(
                    _RecordingMiddleware(host_middleware.name)
                )
            return True

    plugin = _LateContributionPlugin()
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=LlmRegistry(),
        policies=(host_policy,),
        middlewares=(host_middleware,),
        discovery=_discovery(plugin),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(PluginActivationError) as caught:
            await manager.activate((plugin.manifest.plugin_id,))
    finally:
        await manager.dispose()

    assert [failure.code for failure in caught.value.failures] == [
        "plugin-health-check-failed"
    ]
    assert plugin.health_calls == 1
    assert manager.enabled_plugin_ids == ()


@pytest.mark.parametrize("kind", ("tool", "provider", "policy", "middleware"))
async def test_health_check_cannot_mutate_registered_capability_identity(
    kind: str,
) -> None:
    registered_name = f"registered-{kind}"
    conflicting_name = f"host-{kind}"
    if kind == "tool":
        capability = RecordingTool(registered_name)
    elif kind == "provider":
        capability = _Provider(
            registered_name,
            (ModelResponse(content="unused"),),
        )
    elif kind == "policy":
        capability = _RecordingPolicy(registered_name)
    else:
        capability = _RecordingMiddleware(registered_name)

    class _MutableIdentityPlugin:
        manifest = manifest(f"mutable-{kind}.extension")

        def __init__(self) -> None:
            self.health_calls = 0
            self.cleanup_calls = 0

        async def setup(self, context, config) -> None:
            del config
            register = getattr(context, f"register_{kind}")
            register(capability)

            async def cleanup() -> None:
                self.cleanup_calls += 1

            context.add_cleanup(cleanup)

        async def health_check(self, context) -> bool:
            del context
            self.health_calls += 1
            capability.name = conflicting_name
            return True

    tools = ToolRegistry()
    llms = LlmRegistry()
    policies = ()
    middlewares = ()
    if kind == "tool":
        tools.register(RecordingTool(conflicting_name))
    elif kind == "provider":
        llms.register(
            _Provider(conflicting_name, (ModelResponse(content="unused"),))
        )
    elif kind == "policy":
        policies = (_RecordingPolicy(conflicting_name),)
    else:
        middlewares = (_RecordingMiddleware(conflicting_name),)

    plugin = _MutableIdentityPlugin()
    manager = PluginManager(
        tools=tools,
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=llms,
        policies=policies,
        middlewares=middlewares,
        discovery=_discovery(plugin),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(PluginActivationError) as caught:
            await manager.activate((plugin.manifest.plugin_id,))
    finally:
        await manager.dispose()

    assert [failure.code for failure in caught.value.failures] == [
        "plugin-contribution-identity-changed"
    ]
    assert plugin.health_calls == 1
    assert plugin.cleanup_calls == 1
    assert manager.enabled_plugin_ids == ()


@pytest.mark.parametrize("kind", ("tool", "provider"))
async def test_registry_cleanup_uses_the_registration_time_name(kind: str) -> None:
    registered_name = f"registered-{kind}"
    if kind == "tool":
        capability = RecordingTool(registered_name)
        registry = ToolRegistry()
    else:
        capability = _Provider(
            registered_name,
            (ModelResponse(content="unused"),),
        )
        registry = LlmRegistry()

    registration = registry.register(capability)
    capability.name = f"renamed-{kind}"
    await registration.dispose()

    assert registry.get(registered_name) is None


async def test_identity_drift_during_awaited_publish_is_rejected() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GatedServiceRegistry(ServiceRegistry):
        async def provide(self, key, value, *, replace=False):
            entered.set()
            await release.wait()
            return await super().provide(key, value, replace=replace)

    policy = _RecordingPolicy("registered-policy")

    class _AwaitBoundaryPlugin:
        manifest = manifest("await-boundary.extension")

        async def setup(self, context, config) -> None:
            del config
            context.register_policy(policy)
            await context.provide(ServiceKey("candidate-service"), object())

        async def health_check(self, context) -> bool:
            async def mutate_after_publish_waits() -> None:
                await entered.wait()
                policy.name = "renamed-during-publish"
                release.set()

            context.spawn_owned(
                mutate_after_publish_waits(),
                name="identity-drift-fixture",
            )
            return True

    plugin = _AwaitBoundaryPlugin()
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=_GatedServiceRegistry(),
        llms=LlmRegistry(),
        discovery=_discovery(plugin),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(PluginActivationError) as caught:
            await manager.activate((plugin.manifest.plugin_id,))
    finally:
        release.set()
        await manager.dispose()

    assert entered.is_set()
    assert [failure.code for failure in caught.value.failures] == [
        "plugin-contribution-identity-changed"
    ]
    assert manager.enabled_plugin_ids == ()


async def test_public_candidate_rejects_identity_drift_before_generation_handoff() -> None:
    release = asyncio.Event()
    mutated = asyncio.Event()
    tool = RecordingTool("registered-tool")

    class _PostPrepareMutationPlugin:
        manifest = manifest("post-prepare-mutation.extension")

        def __init__(self) -> None:
            self.cleanup_calls = 0

        async def setup(self, context, config) -> None:
            del config
            context.register_tool(tool)

            async def mutate_after_public_prepare() -> None:
                await release.wait()
                tool.name = "renamed-after-prepare"
                mutated.set()

            context.spawn_owned(
                mutate_after_public_prepare(),
                name="post-prepare-identity-mutation-fixture",
            )

            async def cleanup() -> None:
                self.cleanup_calls += 1

            context.add_cleanup(cleanup)

    provider = ScriptedLlmProvider((ModelResponse(content="unused"),))
    llms = LlmRegistry()
    llms.register(provider)
    plugin = _PostPrepareMutationPlugin()
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=llms,
        discovery=_discovery(plugin),  # type: ignore[arg-type]
    )
    activation_set = await manager.prepare_activation_set(
        (plugin.manifest.plugin_id,)
    )
    release.set()
    await mutated.wait()

    sessions = SessionService(InMemoryEventStore())
    try:
        assert tuple(schema.name for schema in activation_set.tools.schemas()) == (
            "registered-tool",
        )
        with pytest.raises(
            ValueError,
            match="capability identity changed after preparation",
        ):
            CompositionGeneration(
                llms=activation_set.llms,
                tools=ToolRuntime(
                    activation_set.tools,
                    sessions,
                    policies=activation_set.policies,
                    middlewares=activation_set.middlewares,
                ),
                prompt=activation_set.prompt,
                provider=provider.name,
                model="candidate-model",
                plugins=activation_set.identities,
                verifier=activation_set.verifier,
                activation_set=activation_set,
            )
        assert activation_set.claimed_by is None
        assert activation_set.tools.get("registered-tool") is tool
        assert activation_set.tools.get("renamed-after-prepare") is None
    finally:
        await activation_set.dispose()

    assert plugin.cleanup_calls == 1
    assert activation_set.state == "disposed"


async def test_activation_set_registry_must_contain_the_selected_provider() -> None:
    provider = _Provider("selected-provider", (ModelResponse(content="unused"),))
    runtime_llms = LlmRegistry()
    runtime_llms.register(provider)
    activation_llms = LlmRegistry()
    tools = ToolRegistry()
    prompt = PromptAssembler()
    services = ServiceRegistry()
    activation_set = PluginActivationSet.empty(
        tools=tools,
        prompt=prompt,
        services=services,
        llms=activation_llms,
    )
    sessions = SessionService(InMemoryEventStore())
    try:
        with pytest.raises(ValueError, match="ActivationSet provider"):
            CompositionGeneration(
                llms=runtime_llms,
                tools=ToolRuntime(tools, sessions, policies=(), middlewares=()),
                prompt=prompt,
                provider=provider.name,
                model="selected-model",
                plugins=activation_set.identities,
                activation_set=activation_set,
            )
    finally:
        await activation_set.dispose()


async def test_policy_overlay_failure_names_the_responsible_plugin(
    tmp_path: Path,
) -> None:
    policy_name = "owned-policy-overlay"
    plugin = ScriptedPlugin(
        manifest("policy-owner.extension"),
        policies=(_RecordingPolicy(policy_name),),
        has_health_check=True,
        health_result=True,
    )
    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            policy_bindings=(
                ScopedPolicyBinding(
                    ScopeKind.AGENT,
                    _RecordingPolicy(policy_name),
                ),
            ),
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(plugin),
            event_store=InMemoryEventStore(),
        )

    assert [(failure.code, failure.plugin_id) for failure in caught.value.failures] == [
        ("policy-override-requires-replace", plugin.manifest.plugin_id)
    ]
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1


async def test_provider_conflict_fails_before_health_and_preserves_the_host(
    tmp_path: Path,
) -> None:
    host = _Provider("stable-provider", (ModelResponse(content="host"),))
    shadow = _Provider("stable-provider", (ModelResponse(content="shadow"),))
    plugin = ScriptedPlugin(
        manifest("provider-conflict.extension"),
        providers=(shadow,),
        has_health_check=True,
        health_result=True,
    )
    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(
                data_dir=tmp_path / "data",
                provider=host.name,
                model="stable-model",
            ),
            provider=host,
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=_discovery(plugin),
            event_store=InMemoryEventStore(),
        )
    assert [failure.code for failure in caught.value.failures] == [
        "provider-publish-conflict"
    ]
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1
    assert host.calls == 0
    assert shadow.calls == 0


async def test_cancellation_reverses_every_extended_registration() -> None:
    setup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    provider = _Provider("cancel-provider", (ModelResponse(content="unused"),))
    policy = _RecordingPolicy("cancel-policy")
    middleware = _RecordingMiddleware("cancel-middleware")
    verifier = _Verifier("unused")

    class _RegistrationPlugin:
        manifest = manifest("cancellation.extension")

        def __init__(self) -> None:
            self.registrations = []

        async def setup(self, context, config) -> None:
            del config
            self.registrations.extend(
                (
                    context.register_provider(provider),
                    context.register_policy(policy),
                    context.register_middleware(middleware),
                    context.register_verifier("cancel-check", verifier),
                )
            )
            setup_entered.set()
            await setup_gate.wait()

    plugin = _RegistrationPlugin()
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        llms=LlmRegistry(),
        discovery=_discovery(plugin),  # type: ignore[arg-type]
    )
    activating = asyncio.create_task(
        manager.activate((plugin.manifest.plugin_id,))
    )
    await setup_entered.wait()
    activating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activating
    assert len(plugin.registrations) == 4
    assert all(registration.disposed for registration in plugin.registrations)
    assert manager.llms.names() == ()
