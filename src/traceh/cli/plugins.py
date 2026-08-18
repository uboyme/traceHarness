"""Safe CLI projections for plugin discovery and activation diagnostics.

Everything printed here originates in third-party distribution metadata: entry
point names and values, distribution names, requirement strings. That text can
contain newlines, ANSI escapes and bidirectional overrides, so every string in
every rendered structure goes through :func:`escape_for_display` before it
reaches the terminal - the same rule the Timeline and resume-command renderers
already follow.

``list`` and ``inspect`` are metadata-only: they never import a plugin, never
create a session and never call a model. ``doctor`` does import, set up and
health-check the plugins it is given, and then immediately disposes them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from traceh.cli.command_line import escape_for_display
from traceh.plugins import PluginDiscovery, PluginError, PluginManager
from traceh.runtime.prompt import PromptAssembler
from traceh.tools.registry import ToolRegistry


def _safe(value: object) -> object:
    """Escape every string anywhere in the structure, not just the ones we expect."""

    if isinstance(value, str):
        return escape_for_display(value, limit=500)
    if isinstance(value, list | tuple):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(_safe(key)): _safe(item) for key, item in value.items()}
    return value


def _emit(result: dict[str, object], *, json_output: bool) -> None:
    safe_result = _safe(result)
    if not isinstance(safe_result, dict):  # pragma: no cover - _safe preserves dicts
        raise TypeError("plugin CLI result must be an object")
    if json_output:
        print(json.dumps(safe_result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    command = safe_result.get("command", "plugins")
    print(f"traceh plugins {command}")
    records = safe_result.get("plugins", [])
    if not isinstance(records, list) or not records:
        print("  no plugins")
    else:
        for item in records:
            if not isinstance(item, dict):
                continue
            entry_point = item.get("entry_point")
            fallback = entry_point.get("name", "?") if isinstance(entry_point, dict) else "?"
            plugin_id = item.get("plugin_id") or fallback
            state = item.get("state", "unknown")
            distribution = item.get("distribution", {})
            dist_name = distribution.get("name") if isinstance(distribution, dict) else None
            dist_version = distribution.get("version") if isinstance(distribution, dict) else None
            print(f"  {plugin_id}: {state} ({dist_name or 'unknown'} {dist_version or 'unknown'})")
            issues = item.get("issues") or []
            if isinstance(issues, list):
                for issue in issues:
                    if isinstance(issue, dict):
                        print(f"    [{issue.get('code', 'issue')}] {issue.get('message', '')}")
            failure = item.get("failure")
            if isinstance(failure, dict):
                print(f"    [{failure.get('code', 'failure')}] {failure.get('message', '')}")
    notices = safe_result.get("notices", [])
    if isinstance(notices, list):
        for notice in notices:
            if isinstance(notice, dict):
                print(
                    f"  notice {notice.get('plugin_id', '?')}: "
                    f"[{notice.get('code', 'notice')}] {notice.get('message', '')}"
                )
    print(f"ok={str(bool(safe_result.get('ok'))).lower()}")


def list_plugins(*, json_output: bool = False, discovery: PluginDiscovery | None = None) -> int:
    records = (discovery or PluginDiscovery()).discover()
    result = {
        "command": "list",
        "ok": True,
        "safe_discovery_only": True,
        "plugins": [record.to_dict() for record in records],
    }
    _emit(result, json_output=json_output)
    return 0


def inspect_plugin(
    plugin_id: str,
    *,
    json_output: bool = False,
    discovery: PluginDiscovery | None = None,
) -> int:
    records = [
        record
        for record in (discovery or PluginDiscovery()).discover()
        if record.entry_name == plugin_id
    ]
    ok = len(records) == 1 and not records[0].issues
    result = {
        "command": "inspect",
        "ok": ok,
        "safe_discovery_only": True,
        "plugins": [record.to_dict() for record in records],
        "note": (
            "Full PluginManifest validation requires importing an explicitly enabled plugin; "
            "use traceh plugins doctor for setup and health checks."
        ),
    }
    _emit(result, json_output=json_output)
    return 0 if ok else 6


async def doctor_plugins(
    plugin_ids: Iterable[str],
    *,
    json_output: bool = False,
    discovery: PluginDiscovery | None = None,
) -> int:
    """Load, validate, set up, health-check and immediately dispose plugins.

    Uses throwaway registries, so nothing it activates can leak into a real
    runtime, and no session or model call is involved.
    """

    actual_discovery = discovery or PluginDiscovery()
    discovered = actual_discovery.discover()
    requested = tuple(plugin_ids)
    if not requested:
        requested = tuple(
            sorted(
                record.entry_name for record in discovered if record.entry_name != "<metadata>"
            )
        )
    manager = PluginManager(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        discovery=actual_discovery,
    )
    failures: list[dict[str, object]] = []
    try:
        await manager.activate(requested)
    except PluginError as error:
        failures.extend(failure.to_dict() for failure in error.failures)
    finally:
        # Always dispose: doctor must not leave a plugin's owned tasks running
        # after it prints its verdict, whether activation succeeded or not.
        try:
            await manager.dispose()
        except PluginError as error:
            failures.extend(failure.to_dict() for failure in error.failures)

    statuses = [status.to_dict() for status in manager.statuses]
    known_ids = {status.get("plugin_id") for status in statuses}
    for failure in failures:
        plugin_id = failure.get("plugin_id")
        if plugin_id not in known_ids:
            # A failure whose plugin never reached discovery still has to appear
            # in the report; otherwise "not installed" looks like "fine".
            statuses.append(
                {
                    "plugin_id": plugin_id,
                    "state": "failed",
                    "distribution": {"name": None, "version": None},
                    "entry_value": None,
                    "manifest": None,
                    "failure": failure,
                }
            )
    statuses.sort(key=lambda item: str(item.get("plugin_id") or ""))
    result = {
        "command": "doctor",
        "ok": not failures,
        "requested": list(requested),
        "plugins": statuses,
        "notices": [notice.to_dict() for notice in manager.notices],
        "failures": failures,
        "llm_used": False,
        "session_created": False,
    }
    _emit(result, json_output=json_output)
    return 0 if not failures else 7


__all__ = ["doctor_plugins", "inspect_plugin", "list_plugins"]
