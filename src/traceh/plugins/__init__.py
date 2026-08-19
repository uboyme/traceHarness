"""Public Plugin SDK and startup-time discovery API.

A plugin distribution should import everything it needs from this module. The
names re-exported here are the supported surface; reaching into
``traceh.runtime`` or ``traceh.session`` from a plugin is not supported and will
break without notice.
"""

from traceh.api.plugins import (
    CORE_PLUGIN_IDENTITY,
    Plugin,
    PluginContext,
    PluginDependency,
    PluginIdentity,
    PluginManifest,
)
from traceh.api.prompts import PromptSection
from traceh.api.services import Registration, ServiceKey
from traceh.api.tools import EffectKind, Tool, ToolExecutionContext, ToolOutput
from traceh.plugins.discovery import (
    ENTRY_POINT_GROUP,
    DiscoveredPlugin,
    DiscoveryIssue,
    PluginDiscovery,
)
from traceh.plugins.errors import (
    PluginActivationError,
    PluginDiscoveryError,
    PluginDisposeError,
    PluginError,
    PluginFailure,
    PluginValidationError,
)
from traceh.plugins.manager import (
    TRACEH_PLUGIN_API_VERSION,
    PluginActivationSet,
    PluginGenerationBuilder,
    PluginManager,
    PluginNotice,
    PluginStatus,
    validate_manifest,
)
from traceh.plugins.selection import is_plugin_id, resolve_enabled_plugins

__all__ = [
    "CORE_PLUGIN_IDENTITY",
    "ENTRY_POINT_GROUP",
    "TRACEH_PLUGIN_API_VERSION",
    "DiscoveredPlugin",
    "DiscoveryIssue",
    "EffectKind",
    "Plugin",
    "PluginActivationError",
    "PluginActivationSet",
    "PluginContext",
    "PluginDependency",
    "PluginDiscovery",
    "PluginDiscoveryError",
    "PluginDisposeError",
    "PluginError",
    "PluginFailure",
    "PluginGenerationBuilder",
    "PluginIdentity",
    "PluginManager",
    "PluginManifest",
    "PluginNotice",
    "PluginStatus",
    "PluginValidationError",
    "PromptSection",
    "Registration",
    "ServiceKey",
    "Tool",
    "ToolExecutionContext",
    "ToolOutput",
    "is_plugin_id",
    "resolve_enabled_plugins",
    "validate_manifest",
]
