"""Public runtime facade and default composition factory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from traceh.api.json_types import JsonValue
from traceh.api.llm import LlmProvider, ModelResponse
from traceh.api.tools import Tool
from traceh.kernel.hooks import HookDispatcher
from traceh.llm.registry import LlmRegistry
from traceh.llm.runtime import LlmRuntime
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_loop import AgentLoop, TurnResult
from traceh.runtime.composition_runtime import StaticCompositionRuntime
from traceh.runtime.continuation import ContinuationRuntime
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.runtime.request_builder import RequestBuilder
from traceh.runtime.verification import CommandVerifier, CompletionVerifier
from traceh.session.compaction import CompactionService
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.event_store import EventStore
from traceh.session.jsonl import JsonlEventStore
from traceh.session.recovery import RecoveryReport, RecoveryService
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.tools.builtins import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    ShellTool,
)
from traceh.tools.middleware import ToolMiddleware
from traceh.tools.policy import AllowByDefaultPolicy, DangerousShellPolicy, ToolPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    data_dir: Path = Path(".traceh")
    provider: str = "scripted"
    model: str = "scripted-model"
    max_steps: int = 20
    tool_timeout_seconds: float = 60.0
    max_tool_output_chars: int = 24_000
    temperature: float | None = None
    max_output_tokens: int | None = None
    verification_command: str | None = None
    verification_timeout_seconds: float = 60.0
    max_verification_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if self.max_tool_output_chars < 1:
            raise ValueError("max_tool_output_chars must be positive")
        if self.verification_timeout_seconds <= 0:
            raise ValueError("verification_timeout_seconds must be positive")
        if self.max_verification_retries < 0:
            raise ValueError("max_verification_retries cannot be negative")


class AgentAlreadyRunningError(RuntimeError):
    pass


class AgentRuntime:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        sessions: SessionService,
        loop: AgentLoop,
        recovery: RecoveryService,
        surface: SurfaceProjector,
        invariants: CoreInvariantChecker,
        hooks: HookDispatcher,
        compaction: CompactionService,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.loop = loop
        self.recovery = recovery
        self.surface = surface
        self.invariants = invariants
        self.hooks = hooks
        self.compaction = compaction
        self._active: dict[str, asyncio.Task[TurnResult]] = {}
        self._lock = asyncio.Lock()
        self._disposed = False

    async def create_session(
        self,
        workspace: Path,
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> str:
        workspace = workspace.resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise NotADirectoryError(workspace)
        return await self.sessions.create_session(workspace, metadata=metadata or {})

    async def run(self, workspace: Path, task: str) -> TurnResult:
        session_id = await self.create_session(workspace)
        return await self.run_existing(session_id, task)

    async def run_existing(self, session_id: str, task: str) -> TurnResult:
        if self._disposed:
            raise RuntimeError("runtime is disposed")
        async with self._lock:
            existing = self._active.get(session_id)
            if existing is not None and not existing.done():
                raise AgentAlreadyRunningError(session_id)
            task_handle = asyncio.create_task(
                self.loop.run_turn(session_id, task),
                name=f"traceh-turn-{session_id}",
            )
            self._active[session_id] = task_handle
        try:
            return await task_handle
        finally:
            async with self._lock:
                if self._active.get(session_id) is task_handle:
                    self._active.pop(session_id, None)

    async def resume(
        self,
        session_id: str,
        *,
        instruction: str = (
            "Continue the previous task. Re-inspect the workspace and the recovered tool results "
            "before repeating any write or process side effect."
        ),
    ) -> tuple[RecoveryReport, TurnResult]:
        report = await self.recovery.recover(session_id)
        result = await self.run_existing(session_id, instruction)
        return report, result

    async def cancel(self, session_id: str, *, reason: str = "cancel requested") -> bool:
        async with self._lock:
            task = self._active.get(session_id)
        if task is None or task.done():
            return False
        await self.sessions.append_session(
            session_id,
            "runtime/cancel-requested",
            {"reason": reason},
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def dispose(self) -> None:
        self._disposed = True
        async with self._lock:
            tasks = tuple(self._active.values())
            self._active.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def check_invariants(self, session_id: str):
        return self.invariants.check(
            await self.sessions.read_session(session_id),
            await self.sessions.read_effects(session_id),
        )



def build_default_runtime(
    config: RuntimeConfig | None = None,
    *,
    provider: LlmProvider | None = None,
    prompt: PromptAssembler | None = None,
    policies: tuple[ToolPolicy, ...] | None = None,
    tool_middlewares: tuple[ToolMiddleware, ...] = (),
    verifier: CompletionVerifier | None = None,
    continuation: ContinuationRuntime | None = None,
    event_store: EventStore | None = None,
    additional_tools: tuple[Tool, ...] = (),
    include_default_tools: bool = True,
) -> AgentRuntime:
    config = config or RuntimeConfig()
    data_dir = config.data_dir.resolve()
    actual_event_store = event_store or JsonlEventStore(data_dir / "events")
    sessions = SessionService(actual_event_store)
    surface = SurfaceProjector()

    llms = LlmRegistry()
    actual_provider = provider or ScriptedLlmProvider(
        (ModelResponse(content="TraceHarness scripted runtime is ready."),)
    )
    llms.register(actual_provider)
    if actual_provider.name != config.provider:
        raise ValueError(
            f"configured provider {config.provider!r} does not match provider object "
            f"{actual_provider.name!r}"
        )

    tool_registry = ToolRegistry()
    default_tools: tuple[Tool, ...] = (
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        ApplyPatchTool(),
        ShellTool(),
    )
    selected_tools = (default_tools if include_default_tools else ()) + additional_tools
    for tool in selected_tools:
        tool_registry.register(tool)

    effective_policies = policies or (DangerousShellPolicy(), AllowByDefaultPolicy())
    tool_runtime = ToolRuntime(
        tool_registry,
        sessions,
        policies=effective_policies,
        middlewares=tool_middlewares,
        timeout_seconds=config.tool_timeout_seconds,
        max_output_chars=config.max_tool_output_chars,
    )
    request_builder = RequestBuilder(sessions, surface)
    hooks = HookDispatcher()
    effective_verifier = verifier
    if effective_verifier is None and config.verification_command:
        effective_verifier = CommandVerifier(
            config.verification_command,
            config.verification_timeout_seconds,
        )

    composition_runtime = StaticCompositionRuntime(
        llms=llms,
        tools=tool_runtime,
        prompt=prompt or default_coding_prompt(),
        provider=config.provider,
        model=config.model,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
    )
    loop = AgentLoop(
        sessions=sessions,
        compositions=composition_runtime,
        request_builder=request_builder,
        llm_runtime=LlmRuntime(),
        data_dir=data_dir,
        max_steps=config.max_steps,
        continuation=continuation,
        verifier=effective_verifier,
        max_verification_retries=config.max_verification_retries,
        hooks=hooks,
    )
    return AgentRuntime(
        config=config,
        sessions=sessions,
        loop=loop,
        recovery=RecoveryService(sessions),
        surface=surface,
        invariants=CoreInvariantChecker(),
        hooks=hooks,
        compaction=CompactionService(sessions),
    )
