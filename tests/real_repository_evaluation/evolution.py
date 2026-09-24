"""Experiment C tooling: dev evidence plans, background settings, headless suggestion,
validation plans (collection plan section 7).

Every paid step goes through a paired plan so each arm runs in a private worker
that connects directly (no proxy). The headless ``suggest`` step drives the same
``BackgroundOptimizationHost`` the TUI uses and disables proxies in this process
before its one analysis call. Nothing here adopts or evaluates a suggestion.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

MODEL = {
    "provider": "openai-compatible",
    "model": "qwen3.6-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_env": "DASHSCOPE_API_KEY",
    "script": None,
    "retry_policy": {
        "max_attempts": 1,
        "max_elapsed_seconds": 0,
        "base_delay_seconds": 0,
        "max_delay_seconds": 0,
        "retry_after_cap_seconds": 0,
        "jitter_ratio": 0,
    },
    # One request's wait. qwen3.6-plus writes about 18s per 1,000 output tokens,
    # so an 8,192-token answer needs about 148s; 120s cut such answers off and
    # was misread as a hung provider (ADR-0086). 300s covers twice that.
    "timeout_seconds": 300,
}
GATE = {"min_pass_gain": 1, "max_token_ratio": 0.95, "max_tool_call_delta": 2}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _plan(benchmark, case_ids, *, candidate=None, first_arm="baseline"):
    return {
        "format": 1,
        "benchmark_digest": _digest(Path(benchmark) / "benchmark.json"),
        "variants": [
            {"variant_id": "baseline", "role": "baseline", "source": "current"},
            {
                "variant_id": "candidate",
                "role": "candidate",
                "source": "current" if candidate is None else candidate,
            },
        ],
        "model": MODEL,
        "execution": {
            "sandbox_config": "sandbox.json",
            "max_trials": 2 * len(case_ids),
            "timeout_seconds": 1800 * len(case_ids) * 2 + 600,
            "shutdown_seconds": 180,
            "network_mode": "direct",
            "first_arm": first_arm,
        },
        "trials": {
            "repetitions": 1,
            "selection": {"case_ids": list(case_ids), "material_seeds": None},
        },
        "comparison": {
            "format": 3,
            "kind": "text_candidate",
            "requested_modes": ["single", "single"],
            **GATE,
        },
    }


def _write(directory, plan, sandbox, extra=()):
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(sandbox, directory / "sandbox.json")
    for source, name in extra:
        shutil.copyfile(source, directory / name)
    path = directory / "run-plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return {"plan": str(path), "sha256": _digest(path)}


def plans_dev(benchmark, cases, output):
    """One A/A Single pair per dev case (two runs of evidence each) plus the template."""
    output = Path(output)
    result = {"template": None, "plans": []}
    for case_id, sandbox in cases:
        entry = _write(output / case_id, _plan(benchmark, [case_id]), sandbox)
        result["plans"].append({"case_id": case_id, **entry})
    template = _plan(benchmark, [case_id for case_id, _ in cases])
    result["template"] = _write(output / "template", template, cases[0][1])
    return result


def settings(benchmark, template_plan, workspace, output_dir, hours):
    raw = {
        "format": 2,
        "period_id": str(uuid4()),
        "workspace": str(Path(workspace).resolve()),
        "benchmark": str(Path(benchmark).resolve()),
        "run_plan": str(Path(template_plan).resolve()),
        "output": str(Path(output_dir).resolve()),
        "expires_at": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
        "max_episodes": 2,
        "max_control_tokens": 64000,
        "max_observations": 100,
        "cooldown_seconds": 60,
        "suggestion_seconds": 300,
        "max_request_bytes": 200000,
        "analysis": {
            "encoding": "cl100k_base",
            "token_limit": 32000,
            "output_tokens": 6000,
            "safety_tokens": 1024,
            "timeout_seconds": 180,
        },
        "selectors": [["product/execution.py", "CODER_GUIDANCE"]],
    }
    return raw


def _direct_only():
    # ``traceh eval`` loads the key from ``.env`` itself; this step builds its
    # Provider directly, so it uses the same loader. The key is never printed.
    # Loading comes first so a proxy variable in ``.env`` is cleared as well.
    from traceh.cli.env_file import load_env_file

    load_env_file(Path(".env"))
    for key in tuple(os.environ):
        if key.upper().endswith("_PROXY"):
            os.environ.pop(key)
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


async def suggest(settings_path, runs, data_dir, acknowledge=None, dismiss_pending=False):
    """Import dev evaluation runs into the same host the TUI uses and wait for it.

    ``acknowledge`` is the person's note after checking a blocked suggestion, the
    same host call as the F6 button; the period's ledger and spent quota stay.
    ``dismiss_pending`` is the person declining the suggestion awaiting review,
    the same call as F6's close button, so the host can admit the next cluster.
    """
    _direct_only()
    from traceh.chat.background import (
        assemble_background,
        background_proposal_text,
        background_status,
        load_background_settings,
    )
    from traceh.llm.openai_compatible import OpenAICompatibleProvider
    from traceh.session.service import SessionService
    from traceh.session.sqlite import SqliteEventStore

    loaded = load_background_settings(settings_path)
    store = SqliteEventStore(Path(data_dir) / "events")
    provider = OpenAICompatibleProvider(
        base_url=MODEL["base_url"], api_key_env=MODEL["api_key_env"], timeout_seconds=180.0
    )
    host = assemble_background(
        SimpleNamespace(sessions=SessionService(store)),
        loaded,
        provider=provider,
        model=MODEL["model"],
        base_url=MODEL["base_url"],
    )
    try:
        await host.open()
        await host.set_enabled(True)
        for run in runs:
            print("imported", run, await host.observe_evaluation(Path(run)), flush=True)
        if acknowledge is not None:
            await host.acknowledge_blocked(acknowledge)
            print("acknowledged blocked suggestion", flush=True)
        if dismiss_pending:
            episode = (await host.open())["pending_review"]
            await host.dismiss(episode)
            print("dismissed suggestion", episode, flush=True)
        state = await host.wait_idle()
        print(background_status(state), flush=True)
        if state["last_evidence"]:
            print(background_proposal_text(state["last_evidence"]), flush=True)
        return state
    finally:
        await host.aclose()
        await store.aclose()


def plans_validate(candidate, cases, output, repetitions=2):
    """Baseline vs the suggested patch, per validation case and repetition."""
    output = Path(output)
    plans = []
    for material, case_id, sandbox in cases:
        for repetition in range(1, repetitions + 1):
            reference = {"file": "candidate.json", "sha256": _digest(candidate)}
            plan = _plan(
                material,
                [case_id],
                candidate=reference,
                first_arm="baseline" if repetition % 2 else "candidate",
            )
            entry = _write(
                output / f"{case_id}-r{repetition}",
                plan,
                sandbox,
                extra=((candidate, "candidate.json"),),
            )
            plans.append({"case_id": case_id, "repetition": repetition, **entry})
    return plans


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dev = commands.add_parser("plans-dev")
    dev.add_argument("--benchmark", type=Path, required=True)
    dev.add_argument("--case", nargs=2, action="append", metavar=("CASE_ID", "SANDBOX"))
    dev.add_argument("--output", type=Path, required=True)
    conf = commands.add_parser("settings")
    for name in ("benchmark", "template-plan", "workspace", "suggestions", "destination"):
        conf.add_argument("--" + name, type=Path, required=True)
    conf.add_argument("--hours", type=int, required=True)
    run = commands.add_parser("suggest")
    run.add_argument("--settings", type=Path, required=True)
    run.add_argument("--run", type=Path, action="append", required=True)
    run.add_argument("--data-dir", type=Path, required=True)
    run.add_argument("--acknowledge", help="note after checking a blocked suggestion")
    run.add_argument("--dismiss-pending", action="store_true", help="decline the pending one")
    val = commands.add_parser("plans-validate")
    val.add_argument("--candidate", type=Path, required=True)
    val.add_argument("--case", nargs=3, action="append", metavar=("MATERIAL", "CASE_ID", "SANDBOX"))
    val.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.command == "plans-dev":
        print(json.dumps(plans_dev(options.benchmark, options.case, options.output), indent=2))
    elif options.command == "settings":
        raw = settings(
            options.benchmark,
            options.template_plan,
            options.workspace,
            options.suggestions,
            options.hours,
        )
        options.workspace.mkdir(parents=True, exist_ok=True)
        options.destination.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        from traceh.chat.background import load_background_settings

        load_background_settings(options.destination)
        print(options.destination)
    elif options.command == "suggest":
        asyncio.run(
            suggest(
                options.settings,
                options.run,
                options.data_dir,
                options.acknowledge,
                options.dismiss_pending,
            )
        )
    else:
        result = plans_validate(options.candidate, options.case, options.output)
        print(json.dumps(result, indent=2))
