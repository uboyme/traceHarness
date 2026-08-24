from __future__ import annotations

from traceh.api.agents import AgentSpec
from traceh.api.plugins import PluginDependency, PluginManifest
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest


def test_future_extension_protocol_values_are_immutable() -> None:
    spec = AgentSpec(
        preset="reviewer",
        workspace_id="workspace-1",
        capability_grants=("read_file",),
    )
    manifest = PluginManifest(
        plugin_id="example.git",
        version="0.1.0",
        requires_plugins=(PluginDependency("traceh.core", ">=0.3"),),
    )
    request = WorkspaceProvisioningRequest(
        "source-main", "revision-1", WorkspaceAccess.READ_ONLY
    )
    assert spec.capability_grants == ("read_file",)
    assert manifest.requires_plugins[0].plugin_id == "traceh.core"
    assert request.revision == "revision-1"
