from __future__ import annotations

from traceh.api.agents import AgentSpec, Budget
from traceh.api.plugins import PluginDependency, PluginManifest
from traceh.api.workspaces import WorkspaceSnapshot


def test_future_extension_protocol_values_are_immutable() -> None:
    spec = AgentSpec(
        preset="reviewer",
        workspace_id="workspace-1",
        capability_grants=("read_file",),
        budget=Budget(max_children=0),
    )
    manifest = PluginManifest(
        plugin_id="example.git",
        version="0.1.0",
        requires_plugins=(PluginDependency("traceh.core", ">=0.3"),),
    )
    snapshot = WorkspaceSnapshot("snap-1", "workspace-1", "abc")
    assert spec.budget.max_children == 0
    assert manifest.requires_plugins[0].plugin_id == "traceh.core"
    assert snapshot.revision == "abc"
