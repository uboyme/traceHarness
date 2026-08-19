"""Deterministic contracts for Composition Generation, Lease and Drain."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import traceh.runtime.composition_runtime as composition_runtime_module
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.api.prompts import PromptSection
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.llm.registry import LlmRegistry
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.runtime.composition_runtime import (
    CompositionDrainError,
    CompositionGeneration,
    CompositionResourceOwner,
    GenerationCleanupFailure,
    GenerationCompositionRuntime,
)
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.middleware import ToolInvocation
from traceh.tools.policy import AllowByDefaultPolicy, DecisionKind, ToolDecision
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


@dataclass
class _Provider:
    name: str
    content: str
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def complete(self, request):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return ModelResponse(content=self.content)


class _BadWorkspace:
    def __str__(self) -> str:
        raise RuntimeError("workspace conversion failed")


@dataclass
class _MutableSchemaTool:
    name: str = "mutable-schema"
    description: str = "mutable schema test tool"
    input_schema: dict = None
    effect_kind: EffectKind = EffectKind.PURE_READ
    calls: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        if self.input_schema is None:
            self.input_schema = {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "additionalProperties": False,
            }

    async def execute(self, arguments, context) -> ToolOutput:
        del context
        if self.closed:
            raise RuntimeError("tool closed")
        self.calls += 1
        return ToolOutput(content=str(arguments["value"]))


@dataclass(slots=True)
class _MutablePolicy:
    name: str = "policy-v1"

    async def check(self, call, tool, context) -> ToolDecision:
        del call, tool, context
        return ToolDecision(DecisionKind.ALLOW, policy=self.name)


@dataclass(slots=True)
class _MutableMiddleware:
    name: str = "middleware-v1"

    async def invoke(self, invocation: ToolInvocation, call_next) -> ToolOutput:
        del invocation
        return await call_next()


class _ClosablePolicy:
    name = "closable-policy"

    def __init__(self) -> None:
        self.closed = False

    async def check(self, call, tool, context) -> ToolDecision:
        del call, tool, context
        if self.closed:
            raise RuntimeError("policy closed")
        return ToolDecision(DecisionKind.ALLOW, policy=self.name)


class _SlottedClosableProvider:
    __slots__ = ("name", "content", "closed")

    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content
        self.closed = False

    async def complete(self, request):
        del request
        if self.closed:
            raise RuntimeError("slotted shared provider closed")
        return ModelResponse(content=self.content)


class _SlottedClosableTool:
    __slots__ = (
        "name",
        "description",
        "input_schema",
        "effect_kind",
        "closed",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "slotted shared tool"
        self.input_schema = {"type": "object"}
        self.effect_kind = EffectKind.PURE_READ
        self.closed = False

    async def execute(self, arguments, context) -> ToolOutput:
        del arguments, context
        if self.closed:
            raise RuntimeError("slotted shared tool closed")
        return ToolOutput(content="slotted tool result")


class _SlottedClosablePolicy:
    __slots__ = ("name", "closed")

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def check(self, call, tool, context) -> ToolDecision:
        del call, tool, context
        if self.closed:
            raise RuntimeError("slotted shared policy closed")
        return ToolDecision(DecisionKind.ALLOW, policy=self.name)


class _SilentBindingPolicy:
    name = "silent-binding-policy"

    def __init__(self) -> None:
        object.__setattr__(self, "closed", False)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_composition_resource_binding":
            return
        object.__setattr__(self, name, value)

    async def check(self, call, tool, context) -> ToolDecision:
        del call, tool, context
        if self.closed:
            raise RuntimeError("shared policy closed")
        return ToolDecision(DecisionKind.ALLOW, policy=self.name)


class _SecondReadFailsPrompt(PromptAssembler):
    def __init__(self) -> None:
        super().__init__((PromptSection("single-read", "captured once", 10),))
        self.section_reads = 0

    def sections(self) -> tuple[PromptSection, ...]:
        self.section_reads += 1
        if self.section_reads > 1:
            raise RuntimeError("raw prompt was read more than once")
        return super().sections()


class _LegacyActivationSet:
    """D0 ownership contract without D1 Scope capabilities."""

    identities: tuple[PluginIdentity, ...] = ()

    def __init__(self) -> None:
        self.claimed_by: object | None = None
        self.dispose_calls = 0

    def _ensure_claimable(self, owner: object) -> None:
        del owner
        if self.claimed_by is not None:
            raise ValueError("already claimed")

    def _claim_for_generation(self, owner: object) -> None:
        self._ensure_claimable(owner)
        self.claimed_by = owner

    def _unclaim_for_generation(self, owner: object) -> None:
        if self.claimed_by is owner:
            self.claimed_by = None

    async def dispose(self) -> None:
        self.dispose_calls += 1


def _resource_owner(cleanup) -> CompositionResourceOwner:
    return CompositionResourceOwner(cleanup)


def _runtime_parts(
    label: str,
    *,
    sessions: SessionService | None = None,
    provider_name: str | None = None,
    cleanup=None,
    generation_cleanup=None,
    prompt: PromptAssembler | None = None,
) -> tuple[GenerationCompositionRuntime, CompositionGeneration, SessionService]:
    sessions = sessions or SessionService(InMemoryEventStore())
    provider_name = provider_name or f"provider-{label}"
    runtime_provider = _Provider(provider_name, f"response-{label}")
    runtime_llms = LlmRegistry()
    runtime_llms.register(runtime_provider)
    runtime_tools = ToolRuntime(
        ToolRegistry(),
        sessions,
        policies=(),
        middlewares=(),
    )
    runtime_prompt = prompt or PromptAssembler(
        (PromptSection(f"section.{label}", f"prompt-{label}", 10),)
    )
    candidate_provider = _Provider(provider_name, f"response-{label}")
    candidate_llms = LlmRegistry()
    candidate_llms.register(candidate_provider)
    candidate_tools = ToolRuntime(
        ToolRegistry(),
        sessions,
        policies=(),
        middlewares=(),
    )
    candidate_prompt = PromptAssembler(runtime_prompt.sections())
    generation = CompositionGeneration(
        llms=candidate_llms,
        tools=candidate_tools,
        prompt=candidate_prompt,
        provider=candidate_provider.name,
        model=f"model-{label}",
        plugins=(),
        resource_owner=(
            _resource_owner(generation_cleanup)
            if generation_cleanup is not None
            else None
        ),
        cleanup=generation_cleanup,
    )
    runtime = GenerationCompositionRuntime(
        llms=runtime_llms,
        tools=runtime_tools,
        prompt=runtime_prompt,
        provider=runtime_provider.name,
        model=f"model-{label}",
        plugins=(),
        cleanup=cleanup,
    )
    return runtime, generation, sessions


def _lease(runtime: GenerationCompositionRuntime, workspace: Path):
    return runtime.lease(
        workspace=workspace,
        session_id="session",
        turn_id="turn",
        step_id="step",
    )


async def _wait_for_composition_tasks_to_finish() -> None:
    await asyncio.gather(
        *(
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("traceh-composition-")
        )
    )


@pytest.mark.asyncio
async def test_custom_activation_set_without_scope_remains_compatible(
    tmp_path: Path,
) -> None:
    runtime, _unused, sessions = _runtime_parts("legacy-activation-runtime")
    provider = _Provider("legacy-activation-provider", "legacy-response")
    llms = LlmRegistry()
    llms.register(provider)
    activation_set = _LegacyActivationSet()
    candidate = CompositionGeneration(
        llms=llms,
        tools=ToolRuntime(ToolRegistry(), sessions, policies=(), middlewares=()),
        prompt=PromptAssembler((PromptSection("legacy", "legacy", 10),)),
        provider=provider.name,
        model="legacy-model",
        plugins=(),
        activation_set=activation_set,
    )

    try:
        assert candidate.scope is None
        assert candidate.services is None
        await runtime.publish(candidate)
        async with _lease(runtime, tmp_path) as active:
            assert active.scope is None
            assert active.services is None
    finally:
        await runtime.dispose()

    assert activation_set.dispose_calls == 1


@pytest.mark.asyncio
async def test_initial_generation_lease_and_revision_is_content_based(tmp_path: Path) -> None:
    runtime, same_content, _ = _runtime_parts("one")
    try:
        async with _lease(runtime, tmp_path) as active:
            assert active.generation_id == 1
            assert active.provider is runtime.current_generation.provider_instance
            assert active.tools is runtime.current_generation.tools
            assert "prompt-one" in active.snapshot.system_prompt
            initial_revision = active.snapshot.revision
            assert runtime.generation_states == ((1, "current", 1),)

        generation_id = await runtime.publish(same_content)
        assert generation_id == 2
        async with _lease(runtime, tmp_path) as active:
            assert active.generation_id == 2
            assert active.snapshot.revision == runtime.current_generation.snapshot(
                workspace=tmp_path
            ).revision
            assert active.snapshot.revision == initial_revision
    finally:
        await runtime.dispose()
    await _wait_for_composition_tasks_to_finish()


@pytest.mark.asyncio
async def test_generation_captures_mutable_sources_once(tmp_path: Path) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("stable", "stable")
    replacement = _Provider("replacement", "replacement")
    llms = LlmRegistry()
    llms.register(provider)
    registry = ToolRegistry()
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(),
        middlewares=(),
    )
    prompt = PromptAssembler((PromptSection("stable", "stable prompt", 10),))
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider="stable",
        model="stable-model",
        plugins=(),
    )
    try:
        async with _lease(runtime, tmp_path) as first:
            initial_revision = first.snapshot.revision
            assert first.provider.name == provider.name

        prompt.register(PromptSection("mutated", "must not appear", 10))
        llms.register(replacement, replace=True)
        registry._tools["mutated"] = SimpleNamespace(
            name="mutated",
            description="",
            input_schema={},
            effect_kind=EffectKind.PURE_READ,
        )

        async with _lease(runtime, tmp_path) as second:
            assert second.generation_id == first.generation_id
            assert second.snapshot.revision == initial_revision
            assert "must not appear" not in second.snapshot.system_prompt
            assert second.provider.name == provider.name
            assert second.tools.registry.names() == ()
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_generation_freezes_tool_metadata_and_execution_schema(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("tool-provider", "tool-response")
    llms = LlmRegistry()
    llms.register(provider)
    source_tool = _MutableSchemaTool()
    registry = ToolRegistry()
    registry.register(source_tool)
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    prompt = PromptAssembler((PromptSection("tool", "tool prompt", 10),))
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="tool-model",
        plugins=(),
    )
    session_id = await sessions.create_session(tmp_path)
    try:
        async with _lease(runtime, tmp_path) as active:
            frozen_tool = active.tools.registry.require(source_tool.name)
            initial_snapshot = active.snapshot
            assert frozen_tool is not source_tool
            assert initial_snapshot.tools[0].input_schema == source_tool.input_schema
            for field, replacement in (
                ("name", "forged-name"),
                ("description", "forged description"),
                ("input_schema", {"type": "object"}),
                ("effect_kind", EffectKind.WORKSPACE_READ),
            ):
                with pytest.raises(AttributeError, match="frozen composition tool"):
                    setattr(frozen_tool, field, replacement)
                with pytest.raises(AttributeError, match="frozen composition tool"):
                    delattr(frozen_tool, field)
            with pytest.raises(TypeError, match="frozen composition value"):
                frozen_tool.input_schema["properties"]["value"]["type"] = "integer"

            source_tool.input_schema = {
                "type": "object",
                "required": ["other"],
                "properties": {"other": {"type": "integer"}},
                "additionalProperties": False,
            }

            assert active.snapshot.revision == initial_snapshot.revision
            assert active.snapshot.tools[0].input_schema == initial_snapshot.tools[0].input_schema
            context = ToolExecutionContext(
                session_id=session_id,
                turn_id="turn",
                step_id="step",
                tool_call_id="call",
                workspace=tmp_path,
                data_dir=tmp_path,
            )
            results = await active.tools.execute_batch(
                (ToolCall("call", source_tool.name, {"value": "stable"}),),
                context=context,
                composition_revision=active.snapshot.revision,
            )
            assert results[0].status == "succeeded"
            assert source_tool.calls == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_repeated_generation_replacement_keeps_tool_adapter_flat(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("stress-provider", "stress-response")
    llms = LlmRegistry()
    llms.register(provider)
    source_tool = _MutableSchemaTool()
    registry = ToolRegistry()
    registry.register(source_tool)
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=PromptAssembler((PromptSection("stress", "stress prompt", 10),)),
        provider=provider.name,
        model="stress-model",
        plugins=(),
    )
    session_id = await sessions.create_session(tmp_path)
    try:
        first_tool = runtime.current_generation.tools.registry.require(source_tool.name)
        for _ in range(1100):
            await runtime.publish(
                replace(runtime.current_generation, cleanup=None)
            )

        async with _lease(runtime, tmp_path) as active:
            assert active.tools.registry.require(source_tool.name) is first_tool
            context = ToolExecutionContext(
                session_id=session_id,
                turn_id="stress-turn",
                step_id="stress-step",
                tool_call_id="stress-call",
                workspace=tmp_path,
                data_dir=tmp_path,
            )
            results = await active.tools.execute_batch(
                (ToolCall("stress-call", source_tool.name, {"value": "stable"}),),
                context=context,
                composition_revision=active.snapshot.revision,
            )

        assert results[0].status == "succeeded"
        assert source_tool.calls == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_generation_captures_policy_and_middleware_names(tmp_path: Path) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("named-provider", "named-response")
    llms = LlmRegistry()
    llms.register(provider)
    policy = _MutablePolicy()
    middleware = _MutableMiddleware()
    tools = ToolRuntime(
        ToolRegistry(),
        sessions,
        policies=(policy,),
        middlewares=(middleware,),
    )
    prompt = PromptAssembler((PromptSection("names", "names prompt", 10),))
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="names-model",
        plugins=(),
    )
    try:
        async with _lease(runtime, tmp_path) as active:
            initial = active.snapshot
            policy.name = "policy-mutated"
            middleware.name = "middleware-mutated"
            current = active.snapshot
            assert current.revision == initial.revision
            assert current.policies == ("policy-v1",)
            assert current.tool_middlewares == ("middleware-v1",)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_compatibility_mutators_cannot_change_current_generation(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("view-provider", "view-response")
    llms = LlmRegistry()
    llms.register(provider)
    source_tool = _MutableSchemaTool()
    registry = ToolRegistry()
    registry.register(source_tool)
    tools = ToolRuntime(registry, sessions, policies=(), middlewares=())
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=PromptAssembler((PromptSection("view", "view prompt", 10),)),
        provider=provider.name,
        model="view-model",
        plugins=(),
    )
    try:
        async with _lease(runtime, tmp_path) as active:
            assert not hasattr(active.tools.registry, "clear")
            with pytest.raises(AttributeError):
                active.tools.registry.clear()
            initial_revision = active.snapshot.revision

        runtime.tools.registry.clear()
        async with _lease(runtime, tmp_path) as active:
            assert active.snapshot.revision == initial_revision
            assert active.tools.registry.names() == (source_tool.name,)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_cleaned_generations_are_removed_after_drain_and_dispose(
    tmp_path: Path,
) -> None:
    runtime, _, sessions = _runtime_parts("retention")
    try:
        for index in range(10):
            _, candidate, _ = _runtime_parts(
                f"retention-{index}", sessions=sessions
            )
            await runtime.publish(candidate)
        await runtime.drain()
        assert tuple(runtime._records) == (11,)
        assert not hasattr(runtime, "_last_generation")
        await runtime.dispose()
        assert runtime._records == {}
    finally:
        await runtime.dispose()
    gc.collect()


@pytest.mark.asyncio
async def test_publish_rejects_plugin_identity_migration(tmp_path: Path) -> None:
    runtime, initial, _ = _runtime_parts("identity")
    candidate = replace(
        initial,
        plugins=(PluginIdentity("future.plugin", "2.0.0"),),
    )
    try:
        with pytest.raises(ValueError, match="plugin identities"):
            await runtime.publish(candidate)
        assert runtime.current_generation_id == 1
        assert runtime.generation_states == ((1, "current", 0),)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_publish_rejects_foreign_session_service(tmp_path: Path) -> None:
    runtime, _, main_sessions = _runtime_parts("main-session")
    foreign_runtime, foreign_generation, foreign_sessions = _runtime_parts(
        "foreign-session"
    )
    try:
        assert foreign_generation.tools.sessions is foreign_sessions
        assert foreign_generation.tools.sessions is not main_sessions
        with pytest.raises(ValueError, match="runtime session service"):
            await runtime.publish(foreign_generation)
        assert runtime.current_generation_id == 1
        assert await main_sessions.list_sessions() == ()
    finally:
        await runtime.dispose()
        await foreign_runtime.dispose()


@pytest.mark.asyncio
async def test_publish_rejects_current_generation_without_retiring_it(
    tmp_path: Path,
) -> None:
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    runtime, _, _ = _runtime_parts("current", cleanup=cleanup)
    try:
        current = runtime.current_generation
        with pytest.raises(ValueError, match="current generation"):
            await runtime.publish(current)
        assert runtime.current_generation is current
        assert runtime.generation_states == ((1, "current", 0),)
        await runtime.drain()
        assert cleanup_calls == 0
    finally:
        await runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_published_generation_cannot_be_republished_after_cleanup(
    tmp_path: Path,
) -> None:
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    runtime, unclaimed_generation, sessions = _runtime_parts(
        "one-shot-owner", cleanup=cleanup
    )
    other_runtime, _, _ = _runtime_parts("other-owner", sessions=sessions)
    first_generation = runtime.current_generation
    candidate = replace(unclaimed_generation, cleanup=None)
    try:
        await runtime.publish(candidate)
        await runtime.drain()
        assert cleanup_calls == 1

        with pytest.raises(ValueError, match="already bound"):
            await runtime.publish(first_generation)
        with pytest.raises(ValueError, match="already bound"):
            await other_runtime.publish(first_generation)
        assert runtime.current_generation is candidate
        assert cleanup_calls == 1
    finally:
        await runtime.dispose()
        await other_runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_generation_cleanup_requires_an_explicit_resource_owner(
    tmp_path: Path,
) -> None:
    runtime, candidate, _ = _runtime_parts("explicit-owner")

    async def cleanup() -> None:
        return None

    try:
        with pytest.raises(ValueError, match="explicit CompositionResourceOwner"):
            replace(candidate, cleanup=cleanup)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_kind", ("policy", "tool", "provider"))
async def test_cleanup_owner_rejects_unbindable_slotted_aliases(
    shared_kind: str,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    shared_provider = (
        _SlottedClosableProvider("slotted-provider", "provider-result")
        if shared_kind == "provider"
        else None
    )
    shared_tool = (
        _SlottedClosableTool("slotted-tool") if shared_kind == "tool" else None
    )
    shared_policy = (
        _SlottedClosablePolicy("slotted-policy") if shared_kind == "policy" else None
    )

    async def cleanup() -> None:
        if shared_provider is not None:
            shared_provider.closed = True
        if shared_tool is not None:
            shared_tool.closed = True
        if shared_policy is not None:
            shared_policy.closed = True

    def make_generation(owner: CompositionResourceOwner) -> CompositionGeneration:
        provider = shared_provider or _Provider("slotted-support", "support-result")
        llms = LlmRegistry()
        llms.register(provider)
        registry = ToolRegistry()
        if shared_tool is not None:
            registry.register(shared_tool)
        policies = (shared_policy,) if shared_policy is not None else ()
        tools = ToolRuntime(
            registry,
            sessions,
            policies=policies,
            middlewares=(),
        )
        return CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=PromptAssembler(
                (PromptSection(f"slotted.{shared_kind}", "slotted", 10),)
            ),
            provider=provider.name,
            model="slotted-model",
            plugins=(),
            resource_owner=owner,
            cleanup=cleanup,
        )

    first_owner = _resource_owner(cleanup)
    second_owner = _resource_owner(cleanup)
    with pytest.raises(ValueError, match="binding-capable"):
        make_generation(first_owner)
    with pytest.raises(ValueError, match="binding-capable"):
        make_generation(second_owner)
    assert first_owner.claimed_by is None
    assert second_owner.claimed_by is None
    assert not any(
        capability.closed
        for capability in (shared_provider, shared_tool, shared_policy)
        if capability is not None
    )


@pytest.mark.asyncio
async def test_failed_generation_construction_is_retryable_and_cleans_once(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("retry-provider", "retry-result")
    llms = LlmRegistry()
    llms.register(provider)
    tools = ToolRuntime(ToolRegistry(), sessions, policies=(), middlewares=())
    prompt = PromptAssembler((PromptSection("retry", "retry", 10),))
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    owner = _resource_owner(cleanup)
    with pytest.raises(LookupError, match="unknown LLM provider"):
        CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=prompt,
            provider="missing-provider",
            model="retry-model",
            plugins=(),
            resource_owner=owner,
            cleanup=cleanup,
        )
    assert owner.claimed_by is None
    assert getattr(llms, "_composition_resource_binding", None) is None
    assert getattr(tools, "_composition_resource_binding", None) is None
    assert getattr(prompt, "_composition_resource_binding", None) is None

    candidate = CompositionGeneration(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="retry-model",
        plugins=(),
        resource_owner=owner,
        cleanup=cleanup,
    )
    runtime_provider = _Provider("runtime-provider", "runtime-result")
    runtime_llms = LlmRegistry()
    runtime_llms.register(runtime_provider)
    runtime = GenerationCompositionRuntime(
        llms=runtime_llms,
        tools=ToolRuntime(
            ToolRegistry(),
            sessions,
            policies=(),
            middlewares=(),
        ),
        prompt=PromptAssembler((PromptSection("runtime", "runtime", 10),)),
        provider=runtime_provider.name,
        model="runtime-model",
        plugins=(),
    )
    try:
        await runtime.publish(candidate)
        await runtime.dispose()
    finally:
        await runtime.dispose()
    assert owner.claimed_by is not None
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_silent_binding_setter_cannot_hide_a_shared_resource(
    tmp_path: Path,
) -> None:
    del tmp_path
    sessions = SessionService(InMemoryEventStore())
    policy = _SilentBindingPolicy()
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        policy.closed = True

    def make_runtime(label: str) -> GenerationCompositionRuntime:
        provider = _Provider(f"silent-{label}", label)
        llms = LlmRegistry()
        llms.register(provider)
        return GenerationCompositionRuntime(
            llms=llms,
            tools=ToolRuntime(
                ToolRegistry(),
                sessions,
                policies=(policy,),
                middlewares=(),
            ),
            prompt=PromptAssembler(
                (PromptSection(f"silent.{label}", label, 10),)
            ),
            provider=provider.name,
            model=f"silent-model-{label}",
            plugins=(),
            cleanup=cleanup,
        )

    first = make_runtime("first")
    try:
        with pytest.raises(ValueError, match="resource lineage|cleanup owner"):
            make_runtime("second")
        assert policy.closed is False
    finally:
        await first.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_binding_commit_failure_restores_exact_component_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("rollback-provider", "rollback")
    llms = LlmRegistry()
    llms.register(provider)
    registry = ToolRegistry()
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(_ClosablePolicy(),),
        middlewares=(),
    )
    prompt = PromptAssembler((PromptSection("rollback", "rollback", 10),))
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    owner = _resource_owner(cleanup)
    original_set_binding = composition_runtime_module._set_binding

    def fail_after_internal_components(component, binding, *, require_storage=False):
        if component is tools.policies[0]:
            raise ValueError("injected binding failure")
        return original_set_binding(
            component,
            binding,
            require_storage=require_storage,
        )

    monkeypatch.setattr(
        composition_runtime_module,
        "_set_binding",
        fail_after_internal_components,
    )
    with pytest.raises(ValueError, match="injected binding failure"):
        CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=prompt,
            provider=provider.name,
            model="rollback-model",
            plugins=(),
            resource_owner=owner,
        )

    for component in (llms, registry, tools):
        assert hasattr(component, "_composition_resource_binding")
        assert component._composition_resource_binding is None
    assert not hasattr(prompt, "_composition_resource_binding")
    assert owner.claimed_by is None

    replacement = _Provider("rollback-retry-provider", "retry")
    llms.register(replacement)
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=ToolRuntime(registry, sessions, policies=(), middlewares=()),
        prompt=prompt,
        provider=replacement.name,
        model="rollback-retry-model",
        plugins=(),
        resource_owner=owner,
        cleanup=cleanup,
    )
    await runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_runtime_builds_compatibility_views_from_the_frozen_generation(
    tmp_path: Path,
) -> None:
    prompt = _SecondReadFailsPrompt()
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("single-read-provider", "single-read")
    llms = LlmRegistry()
    llms.register(provider)
    tools = ToolRuntime(ToolRegistry(), sessions, policies=(), middlewares=())
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="single-read-model",
        plugins=(),
        cleanup=cleanup,
    )
    try:
        assert prompt.section_reads == 1
        assert "captured once" in runtime.prompt.assemble(workspace=str(tmp_path))
    finally:
        # Disposal intentionally refreshes the legacy compatibility projection
        # from the application-owned source.  This test isolates construction.
        prompt.section_reads = 0
        await runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_policy_alias_propagates_resource_binding_to_new_tool_runtime(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    policy = _ClosablePolicy()
    first_provider = _Provider("policy-first", "first")
    first_llms = LlmRegistry()
    first_llms.register(first_provider)
    first_tools = ToolRuntime(
        ToolRegistry(), sessions, policies=(policy,), middlewares=()
    )
    first_runtime = GenerationCompositionRuntime(
        llms=first_llms,
        tools=first_tools,
        prompt=PromptAssembler((PromptSection("policy-first", "first", 10),)),
        provider=first_provider.name,
        model="policy-first-model",
        plugins=(),
    )
    second_provider = _Provider("policy-second", "second")
    second_llms = LlmRegistry()
    second_llms.register(second_provider)
    second_tools = ToolRuntime(
        ToolRegistry(), sessions, policies=(policy,), middlewares=()
    )

    async def cleanup() -> None:
        policy.closed = True

    try:
        with pytest.raises(ValueError, match="derived"):
            CompositionGeneration(
                llms=second_llms,
                tools=second_tools,
                prompt=PromptAssembler((PromptSection("policy-second", "second", 10),)),
                provider=second_provider.name,
                model="policy-second-model",
                plugins=(),
                resource_owner=_resource_owner(cleanup),
            )
        assert policy.closed is False
    finally:
        await first_runtime.dispose()


@pytest.mark.asyncio
async def test_candidate_cannot_share_resources_from_cleanup_generation(
    tmp_path: Path,
) -> None:
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    runtime, _, _ = _runtime_parts("shared-cleanup", cleanup=cleanup)
    current = runtime.current_generation
    try:
        with pytest.raises(ValueError, match="derived"):
            replace(current, cleanup=None)
        with pytest.raises(ValueError, match="derived"):
            CompositionGeneration(
                llms=runtime.llms,
                tools=runtime.tools,
                prompt=runtime.prompt,
                provider=runtime.provider,
                model=runtime.model,
                plugins=runtime.plugins,
            )
        assert runtime.current_generation is current
        assert cleanup_calls == 0
    finally:
        await runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_cleanup_lineage_cannot_publish_two_derived_candidates(
    tmp_path: Path,
) -> None:
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    first_runtime, _, sessions = _runtime_parts("lineage-one")
    second_runtime, _, _ = _runtime_parts("lineage-two", sessions=sessions)
    _, base, _ = _runtime_parts(
        "lineage-base",
        sessions=sessions,
        generation_cleanup=cleanup,
    )
    try:
        with pytest.raises(ValueError, match="derived"):
            replace(base, cleanup=None)
        with pytest.raises(ValueError, match="derived"):
            replace(base, cleanup=None)

        await first_runtime.publish(base)
        await first_runtime.dispose()
        await second_runtime.dispose()
        assert cleanup_calls == 1
    finally:
        await first_runtime.dispose()
        await second_runtime.dispose()


@pytest.mark.asyncio
async def test_cleanup_lineage_cannot_be_washed_out_by_multiple_replace_hops(
    tmp_path: Path,
) -> None:
    async def cleanup() -> None:
        return None

    runtime, base, _ = _runtime_parts(
        "multihop-lineage",
        generation_cleanup=cleanup,
    )
    try:
        with pytest.raises(ValueError, match="derived"):
            replace(base, cleanup=None)
        with pytest.raises(ValueError, match="derived"):
            replace(base, cleanup=None)
        assert runtime.current_generation_id == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_runtime_constructor_rejects_frozen_cleanup_lineage(
    tmp_path: Path,
) -> None:
    async def cleanup() -> None:
        return None

    runtime, cleanup_generation, sessions = _runtime_parts(
        "constructor-lineage",
        generation_cleanup=cleanup,
    )
    try:
        with pytest.raises(ValueError, match="derived"):
            GenerationCompositionRuntime(
                llms=cleanup_generation.llms,
                tools=cleanup_generation.tools,
                prompt=cleanup_generation.prompt,
                provider=cleanup_generation.provider,
                model=cleanup_generation.model,
                plugins=cleanup_generation.plugins,
            )
        assert sessions is runtime.tools.sessions
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_frozen_tool_lineage_survives_a_new_registry_wrapper(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("wrapped-tool-provider", "wrapped-tool-response")
    llms = LlmRegistry()
    llms.register(provider)
    source_tool = _MutableSchemaTool(name="wrapped-tool")
    source_registry = ToolRegistry()
    source_registry.register(source_tool)
    source_tools = ToolRuntime(
        source_registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        source_tool.closed = True

    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=source_tools,
        prompt=PromptAssembler((PromptSection("wrapped-tool", "source", 10),)),
        provider=provider.name,
        model="wrapped-tool-model",
        plugins=(),
        cleanup=cleanup,
    )
    frozen_tool = runtime.current_generation.tools.registry.require(source_tool.name)
    wrapped_registry = ToolRegistry()
    wrapped_registry.register(frozen_tool)
    wrapped_tools = ToolRuntime(
        wrapped_registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    candidate_provider = _Provider("wrapped-tool-candidate", "candidate")
    candidate_llms = LlmRegistry()
    candidate_llms.register(candidate_provider)
    try:
        with pytest.raises(ValueError, match="derived"):
            CompositionGeneration(
                llms=candidate_llms,
                tools=wrapped_tools,
                prompt=PromptAssembler((PromptSection("wrapped-tool", "candidate", 10),)),
                provider=candidate_provider.name,
                model="candidate-model",
                plugins=(),
            )
        assert cleanup_calls == 0
        result = await frozen_tool.execute({"value": "still-open"}, SimpleNamespace())
        assert result.content == "still-open"
    finally:
        await runtime.dispose()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_provider_lineage_survives_a_new_registry_wrapper(tmp_path: Path) -> None:
    runtime, _, sessions = _runtime_parts("wrapped-provider")
    shared_provider = runtime.current_generation.provider_instance
    candidate_llms = LlmRegistry()
    candidate_llms.register(shared_provider)
    candidate_tools = ToolRuntime(
        ToolRegistry(),
        sessions,
        policies=(),
        middlewares=(),
    )
    try:
        with pytest.raises(ValueError, match="derived"):
            CompositionGeneration(
                llms=candidate_llms,
                tools=candidate_tools,
                prompt=PromptAssembler(
                    (PromptSection("wrapped-provider", "candidate", 10),)
                ),
                provider=shared_provider.name,
                model="candidate-model",
                plugins=(),
                resource_owner=_resource_owner(lambda: asyncio.sleep(0)),
            )
        assert runtime.current_generation_id == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_same_raw_capabilities_can_claim_cleanup_only_once(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("raw-alias-provider", "raw-alias-response")
    llms = LlmRegistry()
    llms.register(provider)
    tool = _MutableSchemaTool(name="raw-alias-tool")
    registry = ToolRegistry()
    registry.register(tool)
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    prompt = PromptAssembler((PromptSection("raw-alias", "raw alias", 10),))
    first_cleanup_calls = 0
    second_cleanup_calls = 0

    async def first_cleanup() -> None:
        nonlocal first_cleanup_calls
        first_cleanup_calls += 1
        tool.closed = True

    async def second_cleanup() -> None:
        nonlocal second_cleanup_calls
        second_cleanup_calls += 1

    first = CompositionGeneration(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="raw-alias-model",
        plugins=(),
        resource_owner=_resource_owner(first_cleanup),
        cleanup=first_cleanup,
    )
    with pytest.raises(ValueError, match="another cleanup owner"):
        CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=prompt,
            provider=provider.name,
            model="raw-alias-model",
            plugins=(),
            resource_owner=_resource_owner(second_cleanup),
            cleanup=second_cleanup,
        )
    first_runtime, _, _ = _runtime_parts("raw-owner-one", sessions=sessions)
    second_runtime, _, _ = _runtime_parts("raw-owner-two", sessions=sessions)
    try:
        await first_runtime.publish(first)
        await first_runtime.dispose()
        assert first_cleanup_calls == 1
        assert second_cleanup_calls == 0
        with pytest.raises(RuntimeError, match="tool closed"):
            await tool.execute({}, SimpleNamespace())
    finally:
        await first_runtime.dispose()
        await second_runtime.dispose()


@pytest.mark.asyncio
async def test_cleanup_lineage_remains_claimed_after_first_runtime_cleanup(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("post-cleanup-provider", "post-cleanup-response")
    llms = LlmRegistry()
    llms.register(provider)
    tool = _MutableSchemaTool(name="post-cleanup-tool")
    registry = ToolRegistry()
    registry.register(tool)
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    prompt = PromptAssembler((PromptSection("post-cleanup", "post cleanup", 10),))
    first_cleanup_calls = 0
    second_cleanup_calls = 0

    async def first_cleanup() -> None:
        nonlocal first_cleanup_calls
        first_cleanup_calls += 1

    async def second_cleanup() -> None:
        nonlocal second_cleanup_calls
        second_cleanup_calls += 1

    first = CompositionGeneration(
        llms=llms,
        tools=tools,
        prompt=prompt,
        provider=provider.name,
        model="post-cleanup-model",
        plugins=(),
        resource_owner=_resource_owner(first_cleanup),
        cleanup=first_cleanup,
    )
    first_runtime, _, _ = _runtime_parts("post-cleanup-owner", sessions=sessions)
    try:
        await first_runtime.publish(first)
        await first_runtime.dispose()
        assert first_cleanup_calls == 1
    finally:
        await first_runtime.dispose()

    del first, first_runtime
    gc.collect()

    with pytest.raises(ValueError, match="another cleanup owner"):
        CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=prompt,
            provider=provider.name,
            model="post-cleanup-model",
            plugins=(),
            resource_owner=_resource_owner(second_cleanup),
            cleanup=second_cleanup,
        )
    assert second_cleanup_calls == 0


@pytest.mark.asyncio
async def test_cleanup_candidate_cannot_derive_from_an_active_old_lease(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())
    provider = _Provider("active-old-provider", "active-old-response")
    llms = LlmRegistry()
    llms.register(provider)
    source_tool = _MutableSchemaTool()
    registry = ToolRegistry()
    registry.register(source_tool)
    tools = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        middlewares=(),
    )
    runtime = GenerationCompositionRuntime(
        llms=llms,
        tools=tools,
        prompt=PromptAssembler((PromptSection("active-old", "active old", 10),)),
        provider=provider.name,
        model="active-old-model",
        plugins=(),
    )
    session_id = await sessions.create_session(tmp_path)
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        source_tool.closed = True

    old_lease = _lease(runtime, tmp_path)
    try:
        old = await old_lease.__aenter__()
        with pytest.raises(ValueError, match="derived from existing capabilities"):
            replace(
                runtime.current_generation,
                resource_owner=_resource_owner(cleanup),
                cleanup=cleanup,
            )

        context = ToolExecutionContext(
            session_id=session_id,
            turn_id="active-old-turn",
            step_id="active-old-step",
            tool_call_id="active-old-call",
            workspace=tmp_path,
            data_dir=tmp_path,
        )
        results = await old.tools.execute_batch(
            (ToolCall("active-old-call", source_tool.name, {"value": "still-open"}),),
            context=context,
            composition_revision=old.snapshot.revision,
        )
        assert results[0].status == "succeeded"
        assert cleanup_calls == 0
    finally:
        await old_lease.__aexit__(None, None, None)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_old_lease_tool_survives_cleanup_of_a_new_raw_generation(
    tmp_path: Path,
) -> None:
    sessions = SessionService(InMemoryEventStore())

    def make_generation(label: str, tool: _MutableSchemaTool, cleanup=None):
        provider = _Provider(f"{label}-provider", f"{label}-response")
        llms = LlmRegistry()
        llms.register(provider)
        registry = ToolRegistry()
        registry.register(tool)
        tools = ToolRuntime(
            registry,
            sessions,
            policies=(AllowByDefaultPolicy(),),
            middlewares=(),
        )
        return CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=PromptAssembler((PromptSection(label, f"{label} prompt", 10),)),
            provider=provider.name,
            model=f"{label}-model",
            plugins=(),
            resource_owner=(_resource_owner(cleanup) if cleanup is not None else None),
            cleanup=cleanup,
        )

    old_tool = _MutableSchemaTool(name="old-tool")
    new_tool = _MutableSchemaTool(name="new-tool")
    final_tool = _MutableSchemaTool(name="final-tool")
    old_generation = make_generation("old", old_tool)
    runtime = GenerationCompositionRuntime(
        llms=old_generation.llms,
        tools=old_generation.tools,
        prompt=old_generation.prompt,
        provider=old_generation.provider,
        model=old_generation.model,
        plugins=(),
    )
    cleanup_finished = asyncio.Event()

    async def cleanup_new() -> None:
        new_tool.closed = True
        cleanup_finished.set()

    new_generation = make_generation("new", new_tool, cleanup=cleanup_new)
    final_generation = make_generation("final", final_tool)
    session_id = await sessions.create_session(tmp_path)
    old_lease = _lease(runtime, tmp_path)
    try:
        old = await old_lease.__aenter__()
        await runtime.publish(new_generation)
        await runtime.publish(final_generation)
        await cleanup_finished.wait()
        assert new_tool.closed

        context = ToolExecutionContext(
            session_id=session_id,
            turn_id="old-turn",
            step_id="old-step",
            tool_call_id="old-call",
            workspace=tmp_path,
            data_dir=tmp_path,
        )
        results = await old.tools.execute_batch(
            (ToolCall("old-call", old_tool.name, {"value": "still-open"}),),
            context=context,
            composition_revision=old.snapshot.revision,
        )
        assert results[0].status == "succeeded"
    finally:
        await old_lease.__aexit__(None, None, None)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_lease_is_single_use_and_concurrent_exit_releases_once(
    tmp_path: Path,
) -> None:
    runtime, _, _ = _runtime_parts("single-use")
    lease = _lease(runtime, tmp_path)
    await lease.__aenter__()
    try:
        async with runtime._lock:
            first_exit = asyncio.create_task(lease.__aexit__(None, None, None))
            second_exit = asyncio.create_task(lease.__aexit__(None, None, None))
            await asyncio.sleep(0)
        await asyncio.gather(first_exit, second_exit)
        with pytest.raises(RuntimeError, match="single-use"):
            await lease.__aenter__()
        assert runtime.generation_states == ((1, "current", 0),)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_concurrent_lease_exit_observes_one_release_failure(tmp_path: Path) -> None:
    runtime, _, _ = _runtime_parts("release-failure")
    lease = _lease(runtime, tmp_path)
    await lease.__aenter__()
    original_release = runtime._release

    async def failing_release(record) -> None:
        del record
        raise RuntimeError("release failure")

    runtime._release = failing_release
    try:
        outcomes = await asyncio.gather(
            lease.__aexit__(None, None, None),
            lease.__aexit__(None, None, None),
            return_exceptions=True,
        )
        assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
        assert [str(outcome) for outcome in outcomes] == [
            "release failure",
            "release failure",
        ]
        assert lease._release_task is not None
        assert not lease._released
        assert runtime.generation_states == ((1, "current", 1),)
    finally:
        runtime._release = original_release
        assert lease._record is not None
        await original_release(lease._record)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_cleanup_error_type_is_terminal_safe(tmp_path: Path) -> None:
    unsafe_error = type("Bad\x1b[2J\nForged", (RuntimeError,), {})

    async def cleanup() -> None:
        raise unsafe_error("details are not reported")

    runtime, candidate, _ = _runtime_parts("safe-error", cleanup=cleanup)
    try:
        await runtime.publish(candidate)
        with pytest.raises(CompositionDrainError) as caught:
            await runtime.drain()
        assert "\x1b" not in str(caught.value)
        assert "\n" not in str(caught.value)
        assert caught.value.failures == (
            GenerationCleanupFailure(1, "Bad__2J_Forged"),
        )
    finally:
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()


@pytest.mark.asyncio
async def test_generation_cleanup_tasks_have_no_unretrieved_failures(
    tmp_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def cleanup() -> None:
        raise RuntimeError("cleanup failure")

    runtime, candidate, _ = _runtime_parts("task-outcome", cleanup=cleanup)
    try:
        await runtime.publish(candidate)
        with pytest.raises(CompositionDrainError):
            await runtime.drain()
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()
        await _wait_for_composition_tasks_to_finish()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()


@pytest.mark.asyncio
async def test_publish_freezes_old_lease_and_new_leases_use_new_generation(tmp_path: Path) -> None:
    runtime, _, sessions = _runtime_parts("v1")
    _, generation_two, _ = _runtime_parts("v2", sessions=sessions)
    try:
        old_lease = _lease(runtime, tmp_path)
        old = await old_lease.__aenter__()
        new_id = await runtime.publish(generation_two)

        assert old.generation_id == 1
        assert old.snapshot.model == "model-v1"
        assert old.snapshot.system_prompt.find("prompt-v1") >= 0
        assert runtime.generation_states[0] == (1, "retired", 1)

        async with _lease(runtime, tmp_path) as new:
            assert new.generation_id == new_id
            assert new.snapshot.model == "model-v2"
            assert new.provider is generation_two.provider_instance
            assert new.tools is generation_two.tools

        await old_lease.__aexit__(None, None, None)
        await runtime.drain()
        assert runtime.generation_states == ((2, "current", 0),)
    finally:
        await old_lease.__aexit__(None, None, None)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_last_lease_controls_cleanup_and_multiple_leases_count(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    runtime, generation_two, _ = _runtime_parts("cleanup", cleanup=cleanup)
    try:
        first_lease = _lease(runtime, tmp_path)
        second_lease = _lease(runtime, tmp_path)
        await first_lease.__aenter__()
        await second_lease.__aenter__()
        await runtime.publish(generation_two)
        assert calls == 0

        await first_lease.__aexit__(None, None, None)
        assert calls == 0
        assert runtime.generation_states[0] == (1, "retired", 1)

        drain = asyncio.create_task(runtime.drain())
        await second_lease.__aexit__(None, None, None)
        await started.wait()
        assert calls == 1
        assert not drain.done()
        release.set()
        await drain
        assert runtime.generation_states == ((2, "current", 0),)

        await runtime.drain()
        assert calls == 1
    finally:
        release.set()
        await first_lease.__aexit__(None, None, None)
        await second_lease.__aexit__(None, None, None)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_lease_body_normal_error_and_cancel_all_release(tmp_path: Path) -> None:
    runtime, generation_two, _ = _runtime_parts("body")
    try:
        async with _lease(runtime, tmp_path):
            pass
        assert runtime.generation_states == ((1, "current", 0),)

        with pytest.raises(ValueError, match="body failure"):
            async with _lease(runtime, tmp_path):
                raise ValueError("body failure")
        assert runtime.generation_states == ((1, "current", 0),)

        with pytest.raises(asyncio.CancelledError):
            async with _lease(runtime, tmp_path):
                raise asyncio.CancelledError
        assert runtime.generation_states == ((1, "current", 0),)

        await runtime.publish(generation_two)
        await runtime.drain()
        assert runtime.generation_states[0][2] == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_lease_enter_failure_and_cancellation_do_not_leak(tmp_path: Path) -> None:
    runtime, _, _ = _runtime_parts("failed-enter")
    try:
        with pytest.raises(RuntimeError, match="workspace conversion failed"):
            async with runtime.lease(
                workspace=_BadWorkspace(),
                session_id="session",
                turn_id="turn",
                step_id="step",
            ):
                raise AssertionError("body must not run")
        assert runtime.generation_states == ((1, "current", 0),)

        async with runtime._lock:
            pending = asyncio.create_task(_lease(runtime, tmp_path).__aenter__())
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert runtime.generation_states == ((1, "current", 0),)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_lease_acquire_cancellation_after_increment_does_not_leak(
    tmp_path: Path,
) -> None:
    runtime, _, _ = _runtime_parts("acquire-cancel")
    lease = _lease(runtime, tmp_path)
    acquired = asyncio.Event()
    release_acquire = asyncio.Event()
    original_acquire = runtime._acquire

    async def gated_acquire(lease):
        record = await original_acquire(lease)
        acquired.set()
        await release_acquire.wait()
        return record

    runtime._acquire = gated_acquire
    entering = asyncio.create_task(lease.__aenter__())
    try:
        await acquired.wait()
        entering.cancel()
        await asyncio.sleep(0)
        assert not entering.done()
        release_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await entering
        assert runtime.generation_states == ((1, "current", 0),)
    finally:
        runtime._acquire = original_acquire
        release_acquire.set()
        await runtime.dispose()


@pytest.mark.asyncio
async def test_lease_exit_absorbs_repeated_cancellation_and_releases(tmp_path: Path) -> None:
    runtime, _, _ = _runtime_parts("exit-cancel")
    lease = _lease(runtime, tmp_path)
    await lease.__aenter__()
    try:
        async with runtime._lock:
            closing = asyncio.create_task(lease.__aexit__(None, None, None))
            await asyncio.sleep(0)
            for _ in range(3):
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert runtime.generation_states == ((1, "current", 0),)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_dispose_drain_waits_for_active_lease_and_repeated_cancel(tmp_path: Path) -> None:
    runtime, _, _ = _runtime_parts("drain-lease")
    lease = _lease(runtime, tmp_path)
    await lease.__aenter__()
    try:
        disposing = asyncio.create_task(runtime.dispose())
        await asyncio.sleep(0)
        assert runtime.generation_states == ((1, "retired", 1),)
        for _ in range(3):
            disposing.cancel()
            await asyncio.sleep(0)
            assert not disposing.done()

        await lease.__aexit__(None, None, None)
        with pytest.raises(asyncio.CancelledError):
            await disposing
        assert runtime.generation_states == ()
    finally:
        await lease.__aexit__(None, None, None)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_drain_repeated_cancellation_waits_for_cleanup(tmp_path: Path) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    runtime, candidate, _ = _runtime_parts("drain-cancel", cleanup=cleanup)
    try:
        await runtime.publish(candidate)
        await cleanup_started.wait()
        draining = asyncio.create_task(runtime.drain())
        await asyncio.sleep(0)
        for _ in range(3):
            draining.cancel()
            await asyncio.sleep(0)
            assert not draining.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await draining
    finally:
        cleanup_release.set()
        await runtime.dispose()


@pytest.mark.asyncio
async def test_drain_waits_for_gate_blocked_cleanup(tmp_path: Path) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    runtime, generation_two, _ = _runtime_parts("drain-cleanup", cleanup=cleanup)
    try:
        await runtime.publish(generation_two)
        await cleanup_started.wait()
        draining = asyncio.create_task(runtime.drain())
        await asyncio.sleep(0)
        assert not draining.done()
        cleanup_release.set()
        await draining
        assert runtime.generation_states == ((2, "current", 0),)
    finally:
        cleanup_release.set()
        await runtime.dispose()


@pytest.mark.asyncio
async def test_cleanup_failure_is_structured_and_does_not_skip_other_generations(
    tmp_path: Path,
) -> None:
    successful_cleanup = asyncio.Event()

    async def failing_cleanup() -> None:
        raise ValueError("untrusted cleanup detail")

    async def successful() -> None:
        successful_cleanup.set()

    runtime, _, _ = _runtime_parts("failure")
    _, failing_generation, _ = _runtime_parts(
        "failure-candidate",
        sessions=runtime.tools.sessions,
        generation_cleanup=failing_cleanup,
    )
    _, successful_generation, _ = _runtime_parts(
        "success",
        sessions=runtime.tools.sessions,
        generation_cleanup=successful,
    )
    _, final_generation, _ = _runtime_parts(
        "final", sessions=runtime.tools.sessions
    )
    try:
        await runtime.publish(failing_generation)
        await runtime.publish(successful_generation)
        await runtime.publish(final_generation)
        await successful_cleanup.wait()
        with pytest.raises(CompositionDrainError) as caught:
            await runtime.drain()
        assert caught.value.failures == (GenerationCleanupFailure(2, "ValueError"),)
        assert "untrusted cleanup detail" not in str(caught.value)
        assert runtime.generation_states == ((4, "current", 0),)
        assert runtime.cleanup_failures == (GenerationCleanupFailure(2, "ValueError"),)
    finally:
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()


@pytest.mark.asyncio
async def test_cleanup_failure_poison_is_bounded_and_rejects_future_publish(
    tmp_path: Path,
) -> None:
    async def failing_cleanup() -> None:
        raise ValueError("failure detail is not retained")

    runtime, candidate, _ = _runtime_parts("poison", cleanup=failing_cleanup)
    try:
        await runtime.publish(candidate)
        with pytest.raises(CompositionDrainError):
            await runtime.drain()
        assert len(runtime.cleanup_failures) == 1
        states_before_rejected_publishes = runtime.generation_states
        for _ in range(100):
            with pytest.raises(RuntimeError, match="poisoned"):
                await runtime.publish(candidate)
        assert len(runtime.cleanup_failures) == 1
        assert runtime.generation_states == states_before_rejected_publishes
    finally:
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()


@pytest.mark.asyncio
async def test_repeated_dispose_does_not_repeat_generation_cleanup(tmp_path: Path) -> None:
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1

    runtime, _, _ = _runtime_parts("dispose", cleanup=cleanup)
    await runtime.dispose()
    await runtime.dispose()
    await runtime.drain()
    assert calls == 1
    assert runtime.current_generation_id is None
    with pytest.raises(RuntimeError, match="disposed"):
        _ = runtime.current_generation
    await _wait_for_composition_tasks_to_finish()


@pytest.mark.asyncio
async def test_publish_and_lease_race_has_one_lock_linearization(tmp_path: Path) -> None:
    runtime, generation_two, _ = _runtime_parts("race-v1")
    lease = _lease(runtime, tmp_path)
    try:
        async with runtime._lock:
            lease_task = asyncio.create_task(lease.__aenter__())
            publish_task = asyncio.create_task(runtime.publish(generation_two))
            await asyncio.sleep(0)
            assert not lease_task.done()
            assert not publish_task.done()
        active = await lease_task
        generation_id = await publish_task
        assert active.generation_id in {1, generation_id}
        assert active.generation_id == 1
        await lease.__aexit__(None, None, None)
        async with _lease(runtime, tmp_path) as current:
            assert current.generation_id == generation_id
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_default_sync_and_async_factories_use_generation_runtime(tmp_path: Path) -> None:
    sync_runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "sync"))
    async_runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "async")
    )
    try:
        assert isinstance(sync_runtime.loop.compositions, GenerationCompositionRuntime)
        assert isinstance(async_runtime.loop.compositions, GenerationCompositionRuntime)
        async with _lease(sync_runtime.loop.compositions, tmp_path) as sync_active:
            assert sync_active.generation_id == 1
        async with _lease(async_runtime.loop.compositions, tmp_path) as async_active:
            assert async_active.generation_id == 1
    finally:
        await sync_runtime.dispose()
        await async_runtime.dispose()


@pytest.mark.asyncio
async def test_real_turn_keeps_one_generation_during_publish_and_rebuilds_requests(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entered = asyncio.Event()
    release = asyncio.Event()
    old_provider = _Provider("scripted", "old", entered, release)
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="old-model"),
        provider=old_provider,
        prompt=PromptAssembler(
            (PromptSection("generation.v1", "prompt-v1", 10),)
        ),
    )
    new_provider = _Provider("scripted-v2", "new")
    new_llms = LlmRegistry()
    new_llms.register(new_provider)
    new_tools = ToolRuntime(
        ToolRegistry(),
        runtime.sessions,
        policies=(),
        middlewares=(),
    )
    new_prompt = PromptAssembler(
        (PromptSection("generation.v2", "prompt-v2", 10),)
    )
    new_generation = CompositionGeneration(
        llms=new_llms,
        tools=new_tools,
        prompt=new_prompt,
        provider="scripted-v2",
        model="new-model",
            plugins=(CORE_PLUGIN_IDENTITY,),
    )
    try:
        old_turn = asyncio.create_task(runtime.run(workspace, "old turn"))
        await entered.wait()
        generation_runtime = runtime.loop.compositions
        await generation_runtime.publish(new_generation)
        release.set()
        await old_turn

        await runtime.run(workspace, "new turn")
        sessions = await runtime.sessions.list_sessions()
        assert len(sessions) == 2
        observed: list[tuple[str, str]] = []
        for session_id in sessions:
            events = await runtime.sessions.read_session(session_id)
            composition_event = next(e for e in events if e.type == "composition/snapshot")
            request_event = next(e for e in events if e.type == "request/snapshot")
            assert composition_event.data["provider"] == request_event.data["provider"]
            assert composition_event.data["model"] == request_event.data["model"]
            request_data = request_event.data["request"]
            assert isinstance(request_data, dict)
            assert composition_event.data["system_prompt"] == request_data["system_prompt"]
            observed.append(
                (
                    str(composition_event.data["model"]),
                    str(composition_event.data["system_prompt"]),
                )
            )
        assert {model for model, _ in observed} == {"old-model", "new-model"}
        prompts = {prompt for _, prompt in observed}
        assert all(marker in "\n".join(prompts) for marker in ("prompt-v1", "prompt-v2"))
        for session_id in sessions:
            assert await verify_request_snapshots(
                runtime.sessions, runtime.surface, session_id
            ) == ()
    finally:
        release.set()
        await runtime.dispose()
