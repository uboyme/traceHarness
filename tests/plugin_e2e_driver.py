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
        "traceh-plugin-creator-skill-plugin": metadata.version(
            "traceh-plugin-creator-skill-plugin"
        ),
        "traceh-python-quality-plugin": metadata.version("traceh-python-quality-plugin"),
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
        "inspect-example": inspect_plugin("traceh.example.skill", json_output=False),
        "doctor-example": await doctor_plugins(["traceh.example.skill"], json_output=False),
        "inspect-python-quality": inspect_plugin(
            "traceh.python.quality", json_output=False
        ),
        "doctor-python-quality": await doctor_plugins(
            ["traceh.python.quality"], json_output=False
        ),
        "inspect-plugin-creator": inspect_plugin(
            "traceh.plugin.creator", json_output=False
        ),
        "doctor-plugin-creator": await doctor_plugins(
            ["traceh.plugin.creator"], json_output=False
        ),
    }

    # 4. A default runtime with no plugins must be unchanged.
    plain = build_default_runtime(RuntimeConfig(data_dir=scratch / "plain"))
    report["plain_runtime"] = {
        "tools": list(plain.loop.compositions.tools.registry.names()),
        "plugins": [identity.to_dict() for identity in plain.plugins],
        "prompt_has_plugin_section": "traceh.example.skill"
        in plain.loop.compositions.prompt.section_ids(),
        "prompt_has_creator_section": "traceh.plugin.creator"
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

    # 6. The L1 Plugin Creator is an independent, source-only skill Wheel.  It
    # reaches the model through the existing Prompt/Tool path and its guide
    # performs no workspace write while the normal Event/Effect evidence closes.
    creator_workspace = scratch / "creator-ws"
    creator_workspace.mkdir(parents=True, exist_ok=True)
    creator_provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="creator-guide",
                        name="traceh_plugin_creator_guide",
                        arguments={"topic": "workflow"},
                    ),
                ),
            ),
            ModelResponse(content="The source-only candidate workflow is ready."),
        )
    )
    creator_runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=scratch / "creator-data"),
        provider=creator_provider,
        enabled_plugins=("traceh.plugin.creator",),
    )
    try:
        creator_result = await creator_runtime.run(
            creator_workspace,
            "read the plugin-candidate workflow",
        )
        creator_session = (await creator_runtime.sessions.list_sessions())[0]
        creator_events = await creator_runtime.sessions.read_session(creator_session)
        creator_effects = await creator_runtime.sessions.read_effects(creator_session)
        creator_calls = [event for event in creator_events if event.type == "tool/call"]
        creator_results = [event for event in creator_events if event.type == "tool/result"]
        creator_intents = [event for event in creator_effects if event.type == "effect/intent"]
        creator_outcomes = [
            event for event in creator_effects if event.type == "effect/outcome"
        ]
        creator_snapshots = [
            event for event in creator_events if event.type == "composition/snapshot"
        ]
        report["plugin_creator"] = {
            "turn": {"reason": creator_result.reason, "steps": creator_result.steps},
            "called_tool_names": [event.data.get("tool_name") for event in creator_calls],
            "pairing": {
                "tool_calls": len(creator_calls),
                "tool_results": len(creator_results),
                "effect_intents": len(creator_intents),
                "effect_outcomes": len(creator_outcomes),
            },
            "result_mentions_candidate_workspace": any(
                "Candidate Workspace" in str(event.data) for event in creator_results
            ),
            "prompt_contains_section": (
                "Plugin Creator Skill"
                in creator_runtime.loop.compositions.prompt.assemble(
                    workspace=str(creator_workspace)
                )
            ),
            "workspace_entries": sorted(path.name for path in creator_workspace.iterdir()),
            "snapshot_plugins": creator_snapshots[-1].data.get("plugins"),
            "invariant_violations": [
                violation.rule
                for violation in creator_runtime.invariants.check(
                    creator_events,
                    creator_effects,
                )
            ],
            "reconstruction_violations": [
                str(item)
                for item in await verify_request_snapshots(
                    creator_runtime.sessions,
                    creator_runtime.surface,
                    creator_session,
                )
            ],
        }
    finally:
        await creator_runtime.dispose()

    # 7. The v0.5 acceptance plugin contributes four capability types from a
    # separate wheel.  Its Policy must deny before a process effect is created,
    # its read-only Tool must execute through the normal ToolRuntime, and its
    # named Verifier must run through the Step's Generation Lease.
    quality_workspace = scratch / "quality-ws"
    quality_workspace.mkdir(parents=True, exist_ok=True)
    command = json.dumps([sys.executable, "-m", "unittest", "-v"])
    (quality_workspace / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'quality-e2e'\n"
        "requires-python = '>=3.12'\n\n"
        "[tool.traceh-python-quality]\n"
        f"test-command = {command}\n",
        encoding="utf-8",
    )
    (quality_workspace / "test_quality.py").write_text(
        "import unittest\n\n"
        "class QualityE2E(unittest.TestCase):\n"
        "    def test_plugin_verifier(self):\n"
        "        self.assertEqual(2 + 3, 5)\n",
        encoding="utf-8",
    )
    quality_provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="blocked-shell",
                        name="shell",
                        arguments={
                            "command": "python -m pip uninstall quality-e2e -y"
                        },
                    ),
                ),
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="project-info", name="python_project_info", arguments={}),
                ),
            ),
            ModelResponse(content="Python project evidence and tests are complete."),
        )
    )
    quality_runtime = await build_default_runtime_async(
        RuntimeConfig(
            data_dir=scratch / "quality-data",
            verifier_name="python-tests",
        ),
        provider=quality_provider,
        enabled_plugins=("traceh.python.quality",),
    )
    try:
        quality_result = await quality_runtime.run(
            quality_workspace,
            "inspect this Python project and verify it",
        )
        quality_session = (await quality_runtime.sessions.list_sessions())[0]
        quality_events = await quality_runtime.sessions.read_session(quality_session)
        quality_effects = await quality_runtime.sessions.read_effects(quality_session)
        quality_tool_results = [
            event for event in quality_events if event.type == "tool/result"
        ]
        quality_verifications = [
            event for event in quality_events if event.type == "verification/result"
        ]
        quality_snapshots = [
            event for event in quality_events if event.type == "composition/snapshot"
        ]
        report["python_quality"] = {
            "turn": {"reason": quality_result.reason, "steps": quality_result.steps},
            "tool_results": [
                {
                    "tool_name": event.data.get("tool_name"),
                    "status": event.data.get("status"),
                    "policy": (
                        event.data.get("data", {}).get("policy")
                        if isinstance(event.data.get("data"), dict)
                        else None
                    ),
                }
                for event in quality_tool_results
            ],
            "verification": {
                "passed": quality_verifications[-1].data.get("passed"),
                "exit_code": quality_verifications[-1].data.get("exit_code"),
            },
            "prompt_contains_section": (
                "python_project_info"
                in quality_runtime.loop.compositions.prompt.assemble(
                    workspace=str(quality_workspace)
                )
            ),
            "snapshot_plugins": quality_snapshots[-1].data.get("plugins"),
            "invariant_violations": [
                violation.rule
                for violation in quality_runtime.invariants.check(
                    quality_events,
                    quality_effects,
                )
            ],
            "reconstruction_violations": [
                str(item)
                for item in await verify_request_snapshots(
                    quality_runtime.sessions,
                    quality_runtime.surface,
                    quality_session,
                )
            ],
        }
    finally:
        await quality_runtime.dispose()
    return report


if __name__ == "__main__":
    scratch_dir = Path(sys.argv[1])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    payload = asyncio.run(main(scratch_dir))
    print("<<<E2E_JSON>>>")
    print(json.dumps(payload, indent=2, default=str))
