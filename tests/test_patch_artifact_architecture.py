"""D1 dependency, authority and future-stage boundary guards."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_type_hints

import traceh.artifacts.capture as capture_module
import traceh.artifacts.git_patch as git_patch_module
import traceh.plugins.manager as plugin_manager_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workspaces.supervision as workspace_supervision_module
from traceh.api.agents import AgentSupervisor
from traceh.api.artifacts import ArtifactCas, PatchArtifact, WorkspaceCaptureGate
from traceh.artifacts import (
    ArtifactReportingAgentSupervisor,
    PatchCaptureService,
)
from traceh.workspaces import WorkspaceService


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def _strings(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_artifact_domain_uses_narrow_public_seams() -> None:
    capture_hints = get_type_hints(PatchCaptureService.__init__)
    assert capture_hints["gate"] is WorkspaceCaptureGate
    assert capture_hints["workspace"] is WorkspaceService
    assert capture_hints["cas"] is ArtifactCas
    assert get_type_hints(ArtifactReportingAgentSupervisor.__init__)[
        "inner"
    ] is AgentSupervisor
    assert PatchArtifact.__module__ == "traceh.api.artifacts"


def test_artifact_domain_does_not_import_execution_or_plugin_owners() -> None:
    forbidden = {
        "traceh.runtime.agent_loop",
        "traceh.runtime.agent_runtime",
        "traceh.supervision.supervisor",
        "traceh.plugins.manager",
    }
    artifacts_root = Path(capture_module.__file__).parent
    for source in artifacts_root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert forbidden.isdisjoint(imported)


def test_execution_and_plugin_owners_do_not_import_artifact_control() -> None:
    for module in (
        agent_loop_module,
        agent_runtime_module,
        supervisor_module,
        plugin_manager_module,
        workspace_supervision_module,
        tools_module,
    ):
        assert not any(
            name == "traceh.artifacts" or name.startswith("traceh.artifacts.")
            for name in _imports(module)
        )


def test_capture_git_plumbing_cannot_apply_or_promote_a_patch() -> None:
    literals = _strings(git_patch_module)
    for forbidden in (
        "apply",
        "commit",
        "checkout",
        "merge",
        "reset",
        "update-ref",
    ):
        assert forbidden not in literals


def test_d1_adds_no_model_visible_capture_tool_or_later_stage_types() -> None:
    source = Path(tools_module.__file__).read_text(encoding="utf-8")
    assert "capture_patch" not in source
    assert "PatchCaptureService" not in source
    assert not hasattr(__import__("traceh.api.artifacts", fromlist=["x"]), "PatchReviewReport")
    assert not hasattr(__import__("traceh.api.artifacts", fromlist=["x"]), "PatchApproval")
