"""Opt-in AO-3 real Chat -> feedback -> original AO -> evidence replay.

Case choices and all numerical limits are explicit test inputs, not product defaults.
No user workspace is replayed. The existing profile supplies credentials privately.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from live_optimization.reopen import replay
from live_unified_evaluation.baseline import connection

from traceh.api.json_types import fingerprint, to_json_value
from traceh.chat.activity import default_clock
from traceh.chat.background import (
    assemble_background,
    load_background_settings,
    submit_background_feedback,
)
from traceh.chat.driver import ChatDriver
from traceh.evaluation.evaluators.episode_manifest import load_episode_suite
from traceh.evaluation.evaluators.episode_setup import episode_runtime, setup_episode
from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.strategy import inspect_strategy_optimization
from traceh.llm.retry import NO_MODEL_RETRY


async def execute(options):
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    args, provider, model = connection(options.profile)
    manifest = load_benchmark_manifest(options.benchmark.resolve())
    cases = load_episode_suite(manifest).cases
    foreground = next(c for c in cases if c.data["case_id"] == "s-english")
    raw = json.loads((options.benchmark / "run-plan.example.json").read_text(encoding="utf-8"))
    raw["variants"] = [
        {"variant_id": "reference", "role": "baseline", "source": "current"},
        {"variant_id": "experiment", "role": "candidate", "source": "current"},
    ]
    raw["model"].update(
        provider=provider.name,
        model=model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        script=None,
    )
    raw["execution"] = {
        "sandbox_config": None,
        "max_trials": 4,
        "timeout_seconds": 1200,
        "shutdown_seconds": 120,
        "network_mode": "direct",
    }
    raw["trials"] = {
        "repetitions": 1,
        "selection": {
            "case_ids": ["s-direct", "s-absent"],
            "material_seeds": [113],
        },
    }
    raw["comparison"] = {
        "format": 1,
        "min_pass_gain": 1,
        "max_token_ratio": 1.15,
        "max_tool_call_delta": 2,
    }
    write_json(root / "paired-plan.json", raw)
    definition = {
        "format": 1,
        "period_id": "ao3-real-explicit-period",
        "workspace": str(root / "foreground/workspace"),
        "benchmark": str(options.benchmark.resolve()),
        "run_plan": str(root / "paired-plan.json"),
        "output": str(root / "experiments"),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "max_episodes": 1,
        "max_trials": 4,
        "max_control_tokens": 288000,
        "max_observations": 10,
        "cooldown_seconds": 300,
        "episode_seconds": 1800,
        "max_request_bytes": 200000,
        "analysis": {
            "encoding": "cl100k_base",
            "token_limit": 32000,
            "output_tokens": 6000,
            "safety_tokens": 1024,
            "timeout_seconds": 90,
        },
        "judge": {
            "encoding": "cl100k_base",
            "token_limit": 64000,
            "output_tokens": 2048,
            "safety_tokens": 2048,
            "timeout_seconds": 90,
        },
        "selectors": [
            ["runtime/prompt.py", "_REFERENCE_GUIDANCE"],
            ["tools/reference_search.py", "SkillSearchTool.description"],
        ],
    }
    write_json(root / "background.json", definition)
    write_json(
        root / "frozen-test.json",
        {
            "format": 1,
            "source_digest": source_digest(source_files()[1]),
            "driver_digest": fingerprint(
                await asyncio.to_thread(Path(__file__).read_text, encoding="utf-8")
            ),
            "benchmark_digest": manifest.document.sha256,
            "foreground_case_digest": foreground.digest,
            "limits": {"foreground_turns": 1, "proposals": 1, "paired_trials": 4, "judges": 4},
            "scope": "isolated retrieval fixtures; real provider; feedback is not gold",
            "settings_digest": read_input(root, "background.json").sha256,
            "network": "direct",
            "adoption_authorized": False,
        },
    )
    print(json.dumps({"stage": "foreground", "output": str(root), "model": model}), flush=True)
    async with episode_runtime(
        SimpleNamespace(directory=root / "foreground"),
        foreground,
        manifest,
        provider=provider,
        model_id=model,
        retry_policy=NO_MODEL_RETRY,
        sandbox=None,
    ) as prepared:
        await setup_episode(prepared, foreground, manifest)
        host = assemble_background(
            prepared.runtime,
            load_background_settings(root / "background.json"),
            provider=provider,
            model=model,
            base_url=args.base_url,
            api_key=args.tui_api_key,
        )
        await host.open()
        updates = []

        async def sink(update):
            updates.append(type(update).__name__)

        driver = ChatDriver(
            prepared.runtime,
            prepared.session_id,
            sink=sink,
            timeline=False,
            heartbeat_seconds=0,
            clock=default_clock(),
        )
        try:
            outcome = await driver.run_turn(foreground.data["question"])
            if outcome.result is None:
                raise ValueError("foreground-turn-failed")
            write_json(
                root / "foreground-outcome.json",
                {
                    "session_id": prepared.session_id,
                    "result": to_json_value(outcome.result),
                    "updates": updates,
                },
            )
            await host.set_enabled(True)
            await submit_background_feedback(
                host,
                prepared.session_id,
                "请分析一般的 Skill 目录命中后未读正文、拒绝后重复请求的问题。"
                "这只是用户待核实的检索体验反馈，本轮答案不作为标准答案。"
                "只在现有说明文本确有共性问题时提出小修改，否则返回没有候选；"
                "不要让闲聊或已有充分证据的问题也强制搜索。",
            )
            print(json.dumps({"stage": "background-admitted"}), flush=True)
            state = await asyncio.wait_for(host.wait_idle(), 1900)
            write_json(
                root / "background-state.json",
                to_json_value(
                    {k: sorted(v) if isinstance(v, set) else v for k, v in state.items()}
                ),
            )
            assert state["episodes"] == 1 and state["active"] is None
            assert not await submit_background_feedback(host, prepared.session_id, "同一轮重复反馈")
            assert not await host.kick()
            if state["last_evidence"]:
                result = inspect_strategy_optimization(Path(state["last_evidence"]))
                print(
                    json.dumps(
                        {
                            "stage": "settled",
                            "action": result["action"],
                            "reason": result["reason"],
                            "cost": result["cost"],
                        }
                    ),
                    flush=True,
                )
        finally:
            await host.aclose()
            await driver.aclose()
    reopened = await replay(root)
    write_json(root / "reopen.json", reopened)
    print(json.dumps({"stage": "reopened", "output": str(root)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(execute(parser.parse_args()))
