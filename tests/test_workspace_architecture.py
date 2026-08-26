"""Stage C dependency and authority guards for managed workspaces."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_type_hints

import traceh.plugins.manager as plugin_manager_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.workspaces.local_git as local_git_module
import traceh.workspaces.supervision as workspace_supervision_module
from traceh.api.agents import AgentSupervisor
from traceh.api.workspaces import WorkspaceProvider
from traceh.workspaces import WorkspaceManagedAgentSupervisor, WorkspaceService


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def test_workspace_adapter_uses_public_supervisor_and_provider_seams() -> None:
    hints = get_type_hints(WorkspaceManagedAgentSupervisor.__init__)
    assert hints["inner"] is AgentSupervisor
    assert hints["service"] is WorkspaceService
    assert get_type_hints(WorkspaceService.__init__)["provider"] is WorkspaceProvider

    imported = _imports(workspace_supervision_module)
    assert "traceh.supervision.supervisor" not in imported
    assert "traceh.runtime.agent_runtime" not in imported
    assert "traceh.runtime.agent_loop" not in imported


def test_execution_kernel_does_not_import_workspace_control_state() -> None:
    for module in (
        agent_loop_module,
        agent_runtime_module,
        supervisor_module,
        plugin_manager_module,
    ):
        assert not any(
            name == "traceh.workspaces" or name.startswith("traceh.workspaces.")
            for name in _imports(module)
        )


def test_local_git_provider_has_no_broad_cleanup_or_promotion_command() -> None:
    tree = ast.parse(Path(local_git_module.__file__).read_text(encoding="utf-8"))
    string_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "prune" not in string_literals
    assert "update-ref" not in string_literals
    assert "apply" not in string_literals
    # The sole forced removal is fenced by two exact candidate-tree
    # derivations in ``remove_captured``; ordinary ``remove`` still refuses a
    # dirty tree and no generic force/prune surface exists.
    assert sum(value == "--force" for value in string_literals) == 1
    source = Path(local_git_module.__file__).read_text(encoding="utf-8")
    method = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "remove_captured"
    )
    method_text = ast.unparse(method)
    assert method_text.count("_candidate_tree") == 2
    assert "first != candidate_tree" in method_text


def test_workspace_api_does_not_absorb_artifact_or_future_merge_types() -> None:
    import traceh.api.workspaces as api

    assert not hasattr(api, "PatchArtifact")
    assert not hasattr(api, "MergeResult")
    assert not hasattr(api, "WorkspaceSnapshot")
