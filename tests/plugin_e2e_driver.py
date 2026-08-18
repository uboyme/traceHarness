"""Mainline plugin acceptance, executed inside a clean virtual environment.

Run by ``tests/test_plugin_wheel_e2e.py`` with the *venv's* interpreter, not the
development one. Nothing here may import from the ``tests`` package: the only
things importable in that environment are what the wheels installed.

Takes one argument (a scratch directory) and prints a single JSON object. Not
named ``test_*`` so pytest does not collect it.

No API key and no network are involved: the Scripted Provider drives the turn.
"""

from __future__ import annotations

import asyncio
import json
import sys
from importlib import metadata
from pathlib import Path


async def main(scratch: Path) -> dict:
    from traceh.api.llm import ModelResponse, ToolCall
    from traceh.cli.plugins import doctor_plugins, inspect_plugin, list_plugins
    from traceh.llm.scripted import ScriptedLlmProvider
    from traceh.plugins.discovery import ENTRY_POINT_GROUP, PluginDiscovery
    from traceh.runtime.agent_runtime import (
        RuntimeConfig,
        build_default_runtime,
        build_default_runtime_async,
    )
    from traceh.runtime.request_builder import verify_request_snapshots
    from traceh.version import __version__

    report: dict = {"traceh_version": __version__}

    # 1. The real importlib.metadata entry point, from a really installed wheel.
    points = [
        {"name": point.name, "value": point.value}
        for point in metadata.entry_points(group=ENTRY_POINT_GROUP)
    ]
    report["entry_points"] = sorted(points, key=lambda item: item["name"])
    report["installed_versions"] = {
        "traceharness-py": metadata.version("traceharness-py"),
        "traceh-example-skill-plugin": metadata.version("traceh-example-skill-plugin"),
        "packaging": metadata.version("packaging"),
    }

    # 2. Metadata-only discovery finds it without importing it.
    records = PluginDiscovery().discover()
    report["discovered"] = [
        {
            "name": record.entry_name,
            "issues": [issue.code for issue in record.issues],
            "distribution": record.distribution_name,
            "requirement": record.traceh_requirement,
        }
        for record in records
    ]

    # 3. The three CLI surfaces.
    report["cli"] = {
        "list": list_plugins(json_output=False),
        "inspect": inspect_plugin("traceh.example.skill", json_output=False),
        "doctor": await doctor_plugins(["traceh.example.skill"], json_output=False),
    }

    # 4. A default runtime with no plugins must be unchanged.
    plain = build_default_runtime(RuntimeConfig(data_dir=scratch / "plain"))
    report["plain_runtime"] = {
        "tools": list(plain.loop.compositions.tools.registry.names()),
        "plugins": [identity.to_dict() for identity in plain.plugins],
        "prompt_has_plugin_section": "traceh.example.skill"
        in plain.loop.compositions.prompt.section_ids(),
    }

    # 5. The mainline turn: the model sees the plugin tool and calls it.
    workspace = scratch / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")

    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="",
                tool_calls=(ToolCall(id="call-1", name="example_skill_info", arguments={}),),
            ),
            ModelResponse(content="Read the example skill."),
        )
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=scratch / "data"),
        provider=provider,
        enabled_plugins=("traceh.example.skill",),
    )
    try:
        composition = runtime.loop.compositions
        report["model_visible"] = {
            "tool_schemas": [schema.name for schema in composition.tools.registry.schemas()],
            "prompt_contains_section": "Example Skill"
            in composition.prompt.assemble(workspace=str(workspace)),
        }

        result = await runtime.run(workspace, "read the example skill")
        report["turn"] = {"reason": result.reason, "steps": result.steps}

        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        effects = await runtime.sessions.read_effects(session_id)

        calls = [e for e in events if e.type == "tool/call"]
        results = [e for e in events if e.type == "tool/result"]
        intents = [e for e in effects if e.type == "effect/intent"]
        outcomes = [e for e in effects if e.type == "effect/outcome"]
        report["pairing"] = {
            "tool_calls": len(calls),
            "tool_results": len(results),
            "effect_intents": len(intents),
            "effect_outcomes": len(outcomes),
            "called_tool_names": [e.data.get("tool_name") for e in calls],
            "result_mentions_skill": any("Example Skill" in str(e.data) for e in results),
        }

        snapshots = [e for e in events if e.type == "composition/snapshot"]
        report["composition_plugins"] = snapshots[0].data["plugins"] if snapshots else None

        report["invariant_violations"] = [
            violation.rule for violation in runtime.invariants.check(events, effects)
        ]
        report["reconstruction_violations"] = [
            str(item)
            for item in await verify_request_snapshots(
                runtime.sessions, runtime.surface, session_id
            )
        ]

        session_metadata = events[0].data["metadata"]
        report["session_plugin_metadata"] = session_metadata.get("traceh_plugins")
    finally:
        await runtime.dispose()

    report["after_dispose_tools"] = list(runtime.loop.compositions.tools.registry.names())
    return report


if __name__ == "__main__":
    scratch_dir = Path(sys.argv[1])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    payload = asyncio.run(main(scratch_dir))
    print("<<<E2E_JSON>>>")
    print(json.dumps(payload, indent=2, default=str))
