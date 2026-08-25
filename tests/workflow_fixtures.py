"""Real assembly for the v0.7-E Workflow: Supervisor, capture and promotion."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from promotion_fixtures import (
    build_source_repository,
    make_bare_target,
    promotion_targets,
    verification_plan,
)
from supervision_fixtures import RuntimeFactory

from traceh.api.agents import AgentSpec
from traceh.api.artifacts import PatchCaptureLimits
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.promotion import VerifierCommand
from traceh.api.workflow import WorkflowDefinition
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest
from traceh.artifacts import LocalArtifactCas, PatchCaptureService
from traceh.artifacts.reader import PatchArtifactReader
from traceh.promotion import PatchPromotionService
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import ProcessAgentSupervisor
from traceh.workflow import WorkflowService, WorkflowServices
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
)


class EditingProvider:
    """A model that really edits its workspace, then answers.

    The Verification and Approval nodes need a Patch that is not empty, and the
    only honest way to get one is to let the Agent change a tracked file through
    the ordinary Tool path.
    """

    name = "scripted"

    def __init__(self, marker: str = "workflow") -> None:
        self.marker = marker
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        id=f"call-{self.calls}",
                        name="apply_patch",
                        arguments={
                            "path": "tracked.txt",
                            "old_text": "base\n",
                            "new_text": f"{self.marker}\n",
                        },
                    ),
                ),
            )
        del request
        return ModelResponse(content="edited the workspace")


class _WorkspacePolicy:
    def workspace_for_agent(self, spec: AgentSpec) -> WorkspaceProvisioningRequest:
        del spec
        return WorkspaceProvisioningRequest(
            source_id="trusted-source",
            revision="main",
            access=WorkspaceAccess.WRITABLE,
        )


@dataclass(slots=True)
class RecordingResolver:
    """Host resolver: the only place a prompt or key set comes from."""

    specs: dict[str, AgentSpec] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    spec_calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    message_calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def agent_spec(self, binding_id, *, run_id, node_id, map_key):
        del run_id
        self.spec_calls.append((binding_id, node_id, map_key))
        return self.specs[binding_id]

    async def message_content(self, binding_id, *, run_id, node_id, map_key):
        del run_id
        self.message_calls.append((binding_id, node_id, map_key))
        content = self.messages[binding_id]
        return content if map_key is None else f"{content}:{map_key}"

    async def map_keys(self, binding_id, *, run_id, node_id):
        del run_id, node_id
        return self.keys[binding_id]


@dataclass(slots=True)
class WorkflowAssembly:
    store: InMemoryEventStore
    supervisor: WorkspaceManagedAgentSupervisor
    capture: PatchCaptureService
    promotion: PatchPromotionService
    workflow: WorkflowService
    resolver: RecordingResolver
    target: Path

    async def aclose(self) -> None:
        await self.workflow.aclose()
        await self.promotion.aclose()
        await self.capture.aclose()
        await self.supervisor.aclose()


def workflow_plan(marker: str = "workflow"):
    """A fixed host plan that passes only when the Agent's edit really landed."""

    import sys

    return verification_plan(
        VerifierCommand(
            command_id="edit-present",
            argv=(
                sys.executable,
                "-c",
                _CHECK_PROGRAM.replace("MARKER", marker),
            ),
            timeout_ms=60_000,
        )
    )


_CHECK_PROGRAM = r"""
import pathlib, sys
sys.exit(0 if pathlib.Path('tracked.txt').read_text() == 'MARKER\n' else 1)
"""


def _limits() -> PatchCaptureLimits:
    return PatchCaptureLimits(
        max_changed_paths=100,
        max_path_bytes=512,
        max_file_bytes=1024 * 1024,
        max_total_file_bytes=4 * 1024 * 1024,
        max_patch_bytes=4 * 1024 * 1024,
    )


def build_assembly(
    tmp_path: Path,
    *,
    resolver: RecordingResolver | None = None,
    plan=None,
    provider=None,
    store=None,
) -> WorkflowAssembly:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    managed = tmp_path / "managed"
    # One durable log for the whole assembly: the Workflow refuses to compose
    # services that do not share it.
    store = InMemoryEventStore() if store is None else store

    provider_impl = LocalGitWorkspaceProvider(
        managed_root=managed, sources={"trusted-source": source}
    )
    workspaces = WorkspaceService(store, provider_impl)
    factory = RuntimeFactory(
        store, managed, provider=EditingProvider() if provider is None else provider
    )
    supervisor = WorkspaceManagedAgentSupervisor(
        ProcessAgentSupervisor(store=store, factory=factory),
        workspaces,
        workspace_policy=_WorkspacePolicy(),
    )
    cas = LocalArtifactCas(tmp_path / "cas")
    capture = PatchCaptureService(supervisor, workspaces, cas, limits=_limits())
    promotion = PatchPromotionService(
        store,
        PatchArtifactReader(store, cas),
        promotion_targets("main-target", target),
        plan=workflow_plan() if plan is None else plan,
    )
    resolver = RecordingResolver() if resolver is None else resolver
    resolver.specs.setdefault(
        "coder-spec", AgentSpec(preset="coder", workspace_id="workflow-intent")
    )
    resolver.messages.setdefault("coder-message", "do the work")
    workflow = WorkflowService(
        store,
        WorkflowServices(
            supervisor=supervisor, capture=capture, promotion=promotion
        ),
        resolver,
    )
    return WorkflowAssembly(
        store=store,
        supervisor=supervisor,
        capture=capture,
        promotion=promotion,
        workflow=workflow,
        resolver=resolver,
        target=target,
    )


def definition(*nodes) -> WorkflowDefinition:
    return WorkflowDefinition("demo-workflow", tuple(nodes))


__all__ = [
    "EditingProvider",
    "RecordingResolver",
    "WorkflowAssembly",
    "build_assembly",
    "definition",
    "workflow_plan",
]
