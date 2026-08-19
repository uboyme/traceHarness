from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.plugin_fixtures import (
    RecordingTool,
    ScriptedPlugin,
    entry_point_for,
    manifest,
    provider_for,
)
from traceh.api.json_types import JsonValue
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.prompts import PromptSection
from traceh.api.tools import EffectKind, Tool, ToolExecutionContext, ToolOutput
from traceh.kernel.composition_overlays import (
    CompositionOverlayConflictError,
    CompositionOverlayPlan,
)
from traceh.kernel.registry import ServiceRegistry
from traceh.kernel.scope import ScopeKind
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins import PluginManager
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError
from traceh.runtime import (
    RuntimeConfig,
    ScopedPolicyBinding,
    ScopedPromptBinding,
    ScopedToolBinding,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.runtime.composition_runtime import CompositionGeneration
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.tools.policy import DecisionKind, ToolDecision
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


class _Tool:
    input_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    effect_kind = EffectKind.PURE_READ

    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self.description = f"tool-{marker}"
        self.marker = marker
        self.calls = 0

    async def execute(
        self,
        arguments: dict[str, JsonValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments, context
        self.calls += 1
        return ToolOutput(self.marker)


class _Policy:
    def __init__(self, name: str, decision: DecisionKind = DecisionKind.DEFER) -> None:
        self.name = name
        self.decision = decision

    async def check(
        self,
        call: ToolCall,
        tool: Tool,
        context: ToolExecutionContext,
    ) -> ToolDecision:
        del call, tool, context
        return ToolDecision(self.decision, policy=self.name)


class _ValueEqualPolicy(_Policy):
    """Adversarial fixture: different behavior still compares equal."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ValueEqualPolicy)


@pytest.mark.parametrize(
    ("factory", "value"),
    (
        (
            lambda replace: ScopedToolBinding(
                ScopeKind.AGENT,
                _Tool("strict-tool", "agent"),
                replace=replace,
            ),
            "false",
        ),
        (
            lambda replace: ScopedPromptBinding(
                ScopeKind.AGENT,
                PromptSection("strict.prompt", "agent"),
                replace=replace,
            ),
            1,
        ),
        (
            lambda replace: ScopedPolicyBinding(
                ScopeKind.AGENT,
                _Policy("strict-policy"),
                replace=replace,
            ),
            None,
        ),
    ),
)
def test_scoped_composition_replace_requires_a_real_bool(factory, value) -> None:
    with pytest.raises(TypeError, match="replace must be a bool"):
        factory(value)


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ScopedToolBinding(ScopeKind.AGENT, _Tool("   ", "blank")),
        lambda: ScopedPromptBinding(
            ScopeKind.AGENT,
            PromptSection("   ", "blank"),
        ),
        lambda: ScopedPolicyBinding(ScopeKind.AGENT, _Policy("   ")),
    ),
)
def test_scoped_composition_names_cannot_be_blank(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        factory()


@pytest.mark.parametrize(
    ("binding_name", "bindings", "expected_code"),
    (
        (
            "tool",
            {
                "additional_tools": (_Tool("layered", "application"),),
                "tool_bindings": (
                    ScopedToolBinding(ScopeKind.AGENT, _Tool("layered", "agent")),
                ),
            },
            "tool-override-requires-replace",
        ),
        (
            "prompt",
            {
                "prompt_bindings": (
                    ScopedPromptBinding(
                        ScopeKind.APPLICATION,
                        PromptSection("layered.prompt", "application"),
                    ),
                    ScopedPromptBinding(
                        ScopeKind.PRESET,
                        PromptSection("layered.prompt", "preset"),
                    ),
                ),
            },
            "prompt-override-requires-replace",
        ),
        (
            "policy",
            {
                "policies": (_Policy("layered-policy"),),
                "policy_bindings": (
                    ScopedPolicyBinding(
                        ScopeKind.WORKSPACE,
                        _Policy("layered-policy"),
                    ),
                ),
            },
            "policy-override-requires-replace",
        ),
    ),
)
def test_implicit_cross_scope_overrides_have_stable_codes(
    tmp_path: Path,
    binding_name: str,
    bindings: dict[str, object],
    expected_code: str,
) -> None:
    del binding_name
    with pytest.raises(CompositionOverlayConflictError) as caught:
        build_default_runtime(
            RuntimeConfig(data_dir=tmp_path / expected_code),
            include_default_tools=False,
            event_store=InMemoryEventStore(),
            **bindings,
        )
    assert caught.value.code == expected_code
    assert caught.value.scope in {"workspace", "preset", "agent"}
    assert caught.value.existing_scope == "application"


@pytest.mark.asyncio
async def test_same_scope_replacement_is_explicit_and_last_value_wins(
    tmp_path: Path,
) -> None:
    first = _Tool("same-layer", "first")
    second = _Tool("same-layer", "second")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(
            ScopedToolBinding(ScopeKind.AGENT, first),
            ScopedToolBinding(ScopeKind.AGENT, second, replace=True),
        ),
    )
    try:
        assert runtime.loop.compositions.current_generation.tools.registry.require(
            "same-layer"
        ).description == "tool-second"
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("bindings", "expected_code"),
    (
        (
            {
                "tool_bindings": (
                    ScopedToolBinding(ScopeKind.AGENT, _Tool("same", "first")),
                    ScopedToolBinding(ScopeKind.AGENT, _Tool("same", "second")),
                ),
            },
            "tool-already-bound",
        ),
        (
            {
                "prompt_bindings": (
                    ScopedPromptBinding(
                        ScopeKind.PRESET,
                        PromptSection("same.prompt", "first"),
                    ),
                    ScopedPromptBinding(
                        ScopeKind.PRESET,
                        PromptSection("same.prompt", "second"),
                    ),
                ),
            },
            "prompt-already-bound",
        ),
        (
            {
                "policy_bindings": (
                    ScopedPolicyBinding(ScopeKind.WORKSPACE, _Policy("same-policy")),
                    ScopedPolicyBinding(ScopeKind.WORKSPACE, _Policy("same-policy")),
                ),
            },
            "policy-already-bound",
        ),
    ),
)
def test_same_scope_duplicates_require_explicit_replacement(
    tmp_path: Path,
    bindings: dict[str, object],
    expected_code: str,
) -> None:
    with pytest.raises(CompositionOverlayConflictError) as caught:
        build_default_runtime(
            RuntimeConfig(data_dir=tmp_path / expected_code),
            include_default_tools=False,
            event_store=InMemoryEventStore(),
            **bindings,
        )
    assert caught.value.code == expected_code


@pytest.mark.asyncio
async def test_prompt_replacement_cleanup_restores_the_previous_section() -> None:
    previous = PromptSection("shared.prompt", "previous", 10)
    replacement = PromptSection("shared.prompt", "replacement", 20)
    prompt = PromptAssembler((previous,))

    registration = prompt.register(replacement, replace=True)
    assert prompt.sections() == (replacement,)

    await registration.dispose()
    assert prompt.sections() == (previous,)


@pytest.mark.asyncio
async def test_scope_resolution_uses_scope_order_not_input_order(
    tmp_path: Path,
) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "ordered"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(
            ScopedToolBinding(
                ScopeKind.AGENT,
                _Tool("ordered-tool", "agent"),
                replace=True,
            ),
            ScopedToolBinding(
                ScopeKind.APPLICATION,
                _Tool("ordered-tool", "application"),
            ),
        ),
    )
    try:
        assert runtime.loop.compositions.current_generation.tools.registry.require(
            "ordered-tool"
        ).description == "tool-agent"
    finally:
        await runtime.dispose()


def test_failed_overlay_resolution_does_not_mutate_its_inputs() -> None:
    base_tool = _Tool("stable-tool", "base")
    replacement_tool = _Tool("stable-tool", "replacement")
    tools = ToolRegistry()
    tools.register(base_tool)
    base_prompt = PromptSection("stable.prompt", "base")
    prompt = PromptAssembler((base_prompt,))
    base_policy = _Policy("stable-policy")
    plan = CompositionOverlayPlan(
        tool_bindings=(
            ScopedToolBinding(
                ScopeKind.AGENT,
                replacement_tool,
                replace=True,
            ),
        ),
        prompt_bindings=(
            ScopedPromptBinding(
                ScopeKind.AGENT,
                PromptSection("stable.prompt", "replacement"),
                replace=True,
            ),
        ),
        policy_bindings=(
            ScopedPolicyBinding(
                ScopeKind.AGENT,
                _Policy("stable-policy"),
            ),
        ),
    )

    with pytest.raises(CompositionOverlayConflictError) as caught:
        plan.resolve(tools, prompt, (base_policy,))

    assert caught.value.code == "policy-override-requires-replace"
    assert tools.require("stable-tool") is base_tool
    assert prompt.sections() == (base_prompt,)


@pytest.mark.asyncio
async def test_two_runtimes_can_have_different_agent_tool_prompt_and_policy_compositions(
    tmp_path: Path,
) -> None:
    first_tool = _Tool("agent-tool", "first")
    second_tool = _Tool("agent-tool", "second")
    first = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "first"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(ScopedToolBinding(ScopeKind.AGENT, first_tool),),
        prompt_bindings=(
            ScopedPromptBinding(
                ScopeKind.AGENT,
                PromptSection("agent.instructions", "first prompt", 5),
            ),
        ),
        policy_bindings=(
            ScopedPolicyBinding(ScopeKind.AGENT, _Policy("first-policy")),
        ),
    )
    second = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "second"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(ScopedToolBinding(ScopeKind.AGENT, second_tool),),
        prompt_bindings=(
            ScopedPromptBinding(
                ScopeKind.AGENT,
                PromptSection("agent.instructions", "second prompt", 5),
            ),
        ),
        policy_bindings=(
            ScopedPolicyBinding(ScopeKind.AGENT, _Policy("second-policy")),
        ),
    )
    try:
        first_snapshot = first.loop.compositions.current_generation.snapshot(
            workspace=tmp_path
        )
        second_snapshot = second.loop.compositions.current_generation.snapshot(
            workspace=tmp_path
        )
        assert first_snapshot.revision != second_snapshot.revision
        assert "first prompt" in first_snapshot.system_prompt
        assert "second prompt" in second_snapshot.system_prompt
        assert first_snapshot.tools[0].description == "tool-first"
        assert second_snapshot.tools[0].description == "tool-second"
        assert "first-policy" in first_snapshot.policies
        assert "second-policy" in second_snapshot.policies
    finally:
        await first.dispose()
        await second.dispose()


@pytest.mark.asyncio
async def test_agent_policy_overlay_changes_real_tool_admission(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "policy-workspace"
    workspace.mkdir()
    denied_tool = _Tool("policy-tool", "denied")
    allowed_tool = _Tool("policy-tool", "allowed")

    def provider() -> ScriptedLlmProvider:
        return ScriptedLlmProvider(
            (
                ModelResponse(
                    tool_calls=(ToolCall("policy-call", "policy-tool", {}),),
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="done"),
            )
        )

    denied_provider = provider()
    allowed_provider = provider()
    denied = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "denied-data",
            provider=denied_provider.name,
            model="policy-model",
        ),
        provider=denied_provider,
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(
            ScopedToolBinding(ScopeKind.AGENT, denied_tool),
        ),
        policy_bindings=(
            ScopedPolicyBinding(
                ScopeKind.AGENT,
                _Policy("agent-deny", DecisionKind.DENY),
            ),
        ),
    )
    allowed = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "allowed-data",
            provider=allowed_provider.name,
            model="policy-model",
        ),
        provider=allowed_provider,
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(
            ScopedToolBinding(ScopeKind.AGENT, allowed_tool),
        ),
        policy_bindings=(
            ScopedPolicyBinding(
                ScopeKind.AGENT,
                _Policy("agent-allow", DecisionKind.ALLOW),
            ),
        ),
    )
    try:
        denied_session = await denied.create_session(workspace)
        allowed_session = await allowed.create_session(workspace)
        await denied.run_existing(denied_session, "try the policy tool")
        await allowed.run_existing(allowed_session, "use the policy tool")
        assert denied_tool.calls == 0
        assert allowed_tool.calls == 1
    finally:
        await denied.dispose()
        await allowed.dispose()


@pytest.mark.asyncio
async def test_public_manager_candidate_preserves_child_composition_blueprint() -> None:
    agent_tool = _Tool("manager-tool", "agent")
    prompt_section = PromptSection("manager.prompt", "agent prompt")
    policy = _Policy("manager-policy")
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        composition_overlays=CompositionOverlayPlan(
            tool_bindings=(
                ScopedToolBinding(ScopeKind.AGENT, agent_tool),
            ),
            prompt_bindings=(
                ScopedPromptBinding(ScopeKind.PRESET, prompt_section),
            ),
            policy_bindings=(
                ScopedPolicyBinding(ScopeKind.WORKSPACE, policy),
            ),
        ),
    )

    candidate = await manager.prepare_activation_set(())
    try:
        assert candidate.tools.require("manager-tool") is agent_tool
        assert candidate.prompt.sections() == (prompt_section,)
        assert candidate.policies == (policy,)
    finally:
        await candidate.dispose()


@pytest.mark.asyncio
async def test_generation_requires_the_activation_sets_exact_policy_objects(
    tmp_path: Path,
) -> None:
    candidate_policy = _ValueEqualPolicy("identity-policy", DecisionKind.ALLOW)
    foreign_policy = _ValueEqualPolicy("identity-policy", DecisionKind.DENY)
    assert candidate_policy == foreign_policy
    assert candidate_policy is not foreign_policy
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        policies=(candidate_policy,),
    )
    candidate = await runtime._plugin_compositions._plugin_builder.prepare(())
    try:
        foreign_tools = ToolRuntime(
            candidate.tools,
            runtime.sessions,
            policies=(foreign_policy,),
            middlewares=runtime._plugin_compositions._middlewares,
        )
        with pytest.raises(
            ValueError,
            match="must use its ActivationSet policies",
        ):
            CompositionGeneration(
                llms=runtime._plugin_compositions._llms,
                tools=foreign_tools,
                prompt=candidate.prompt,
                provider=runtime.config.provider,
                model=runtime.config.model,
                plugins=candidate.identities,
                activation_set=candidate,
            )
    finally:
        await candidate.dispose()
        await runtime.dispose()


@pytest.mark.asyncio
async def test_agent_tool_overlay_executes_through_the_real_runtime_and_rebuilds(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    application = _Tool("layered-tool", "application")
    agent = _Tool("layered-tool", "agent")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("call-1", "layered-tool", {}),),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done"),
        )
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider=provider.name,
            model="overlay-model",
        ),
        provider=provider,
        include_default_tools=False,
        additional_tools=(application,),
        tool_bindings=(
            ScopedToolBinding(ScopeKind.AGENT, agent, replace=True),
        ),
        event_store=InMemoryEventStore(),
    )
    try:
        session_id = await runtime.create_session(workspace)
        result = await runtime.run_existing(session_id, "use the layered tool")
        assert result.reason == "completed"
        assert application.calls == 0
        assert agent.calls == 1
        assert await verify_request_snapshots(
            runtime.sessions,
            runtime.surface,
            session_id,
        ) == ()
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_plugin_tool_cannot_be_shadowed_implicitly_and_health_never_runs(
    tmp_path: Path,
) -> None:
    plugin_tool = RecordingTool("late-tool", "plugin")
    plugin = ScriptedPlugin(
        manifest("overlay.plugin"),
        tools=(plugin_tool,),
        has_health_check=True,
    )
    discovery = PluginDiscovery(
        entry_points_provider=provider_for(entry_point_for(plugin))
    )

    with pytest.raises(PluginActivationError) as caught:
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            include_default_tools=False,
            event_store=InMemoryEventStore(),
            enabled_plugins=("overlay.plugin",),
            plugin_discovery=discovery,
            tool_bindings=(
                ScopedToolBinding(
                    ScopeKind.AGENT,
                    _Tool("late-tool", "agent"),
                ),
            ),
        )

    assert caught.value.failures[0].code == "tool-override-requires-replace"
    assert caught.value.failures[0].plugin_id == "overlay.plugin"
    assert plugin.health_calls == 0
    assert plugin.cleanup_calls == 1


@pytest.mark.asyncio
async def test_explicit_agent_overlay_wins_over_application_plugin_contribution(
    tmp_path: Path,
) -> None:
    plugin_tool = RecordingTool("late-tool", "plugin")
    agent_tool = _Tool("late-tool", "agent")
    plugin = ScriptedPlugin(
        manifest("overlay.plugin"),
        tools=(plugin_tool,),
        has_health_check=True,
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        enabled_plugins=("overlay.plugin",),
        plugin_discovery=PluginDiscovery(
            entry_points_provider=provider_for(entry_point_for(plugin))
        ),
        tool_bindings=(
            ScopedToolBinding(
                ScopeKind.AGENT,
                agent_tool,
                replace=True,
            ),
        ),
    )
    try:
        effective = runtime.loop.compositions.current_generation.tools.registry.require(
            "late-tool"
        )
        assert effective.description == "tool-agent"
        assert plugin.health_calls == 1
    finally:
        await runtime.dispose()
    assert plugin.cleanup_calls == 1


@pytest.mark.asyncio
async def test_plugin_replacement_preserves_programmatic_child_overlays(
    tmp_path: Path,
) -> None:
    agent_tool = _Tool("agent-only", "agent")
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
        tool_bindings=(ScopedToolBinding(ScopeKind.AGENT, agent_tool),),
        prompt_bindings=(
            ScopedPromptBinding(
                ScopeKind.PRESET,
                PromptSection("preset.instructions", "keep me", 5),
            ),
        ),
        policy_bindings=(
            ScopedPolicyBinding(ScopeKind.WORKSPACE, _Policy("workspace-policy")),
        ),
    )
    plugin = ScriptedPlugin(manifest("replacement.plugin"))
    try:
        await runtime.replace_plugin_composition(
            ("replacement.plugin",),
            plugin_discovery=PluginDiscovery(
                entry_points_provider=provider_for(entry_point_for(plugin))
            ),
        )
        generation = runtime.loop.compositions.current_generation
        snapshot = generation.snapshot(workspace=tmp_path)
        assert (
            generation.tools.registry.require("agent-only").description
            == "tool-agent"
        )
        assert "keep me" in snapshot.system_prompt
        assert "workspace-policy" in snapshot.policies
    finally:
        await runtime.dispose()
