"""Paired single-vs-multi comparison on one frozen task, through the eval CLI.

This uses the repository's own `execution_strategy` comparison: two arms of the
same benchmark, baseline `single` and candidate `multi`, run by `traceh eval`
with one run plan. Nothing here scores anything; the original comparison owner
decides the verdict from the two reports.
"""

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path

from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.autonomy_materials import (
    CHECKS,
    EDITABLE,
    FILES,
    REFERENCE,
    REQUIREMENT,
)
from live_dynamic_collaboration.materials import write
from traceh.evaluation.inputs import digest_bytes

CASE = "strategy-comparison-telemetry"
AUTHORIZED = 4
# Both arms share one profile, so the ceilings must fit the more demanding arm.
TASK_TOKENS = 1_500_000
CODER_TOKENS = 1_200_000
ASSISTANT_TOKENS = 50_000
RETAINED_TOKENS = 40_000
MAIN_TURN_WALL_MS = 600_000
ASSISTANT_WALL_MS = 240_000
CODER_WALL_MS = MAIN_TURN_WALL_MS + AUTHORIZED * ASSISTANT_WALL_MS + 180_000
ASSISTANT_STEPS = 12
ASSISTANT_TOOL_CALLS = 16
CODER_STEPS = 52 + AUTHORIZED * ASSISTANT_STEPS
CODER_TOOL_CALLS = 72 + AUTHORIZED * ASSISTANT_TOOL_CALLS
MODEL = "deepseek-v4-flash"
REPETITIONS = 1
EVAL_TIMEOUT_SECONDS = 2400


def _environment(env_file):
    """Read the authorized non-secret endpoint from the explicit env file."""
    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    base_url = values.get("TRACEH_BASE_URL")
    if not base_url:
        raise ValueError("comparison-base-url-missing")
    return values, base_url


def _select():
    driver.FILES, driver.REFERENCE = FILES, REFERENCE
    driver.CHECKS, driver.REQUIREMENT = CHECKS, REQUIREMENT
    driver.EDITABLE = EDITABLE


def prepare(repository, sandbox, output, env_file):
    values, base_url = _environment(env_file)
    _select()
    driver.prepare(repository, sandbox, output)
    dataset_path = output / "material/dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    case = dataset["cases"][0]
    case.update(case_id=CASE, group_id=CASE)
    argv = case["verification"]["commands"][0]["argv"]
    head, marker, tail = argv[3].partition("assert files==set(fixed)|")
    if not marker or not tail:
        raise ValueError("comparison-verifier-shape-unexpected")
    case["verification"]["commands"][0]["argv"] = [
        *argv[:3],
        head + marker + repr(set(EDITABLE)) + ",files\n" + CHECKS,
    ]
    write(dataset_path, dataset)
    manifest_path = output / "material/benchmark.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = manifest["task_settings"]
    # Both strategies must be permitted by the same frozen profile.
    settings.update(default_mode="single", modes=["single", "multi"])
    settings["retained_tokens"] = RETAINED_TOKENS
    settings["task_budget"].update(
        max_tokens=TASK_TOKENS,
        max_children=AUTHORIZED,
        max_wall_milliseconds=CODER_WALL_MS + 180_000,
        max_steps=CODER_STEPS + 24,
        max_tool_calls=CODER_TOOL_CALLS + 24,
        max_processes=1 + AUTHORIZED,
    )
    settings["roles"]["coder"]["budget"].update(
        max_tokens=CODER_TOKENS,
        max_children=AUTHORIZED,
        max_wall_milliseconds=CODER_WALL_MS,
        max_steps=CODER_STEPS,
        max_tool_calls=CODER_TOOL_CALLS,
        max_processes=AUTHORIZED,
    )
    settings["roles"]["coder"]["max_turn_wall_milliseconds"] = MAIN_TURN_WALL_MS
    settings["roles"]["patch_author"]["budget"] = dict(
        deepcopy(settings["roles"]["patch_author"]["budget"]),
        max_tokens=ASSISTANT_TOKENS,
        max_steps=ASSISTANT_STEPS,
        max_tool_calls=ASSISTANT_TOOL_CALLS,
        max_wall_milliseconds=ASSISTANT_WALL_MS,
        max_children=0,
        max_depth=0,
        max_processes=0,
    )
    manifest["dataset"]["sha256"] = digest_bytes(dataset_path.read_bytes())
    write(manifest_path, manifest)
    plan = {
        "format": 1,
        "benchmark_digest": digest_bytes(manifest_path.read_bytes()),
        "variants": [
            {"variant_id": "single-arm", "role": "baseline", "source": "current"},
            {"variant_id": "multi-arm", "role": "candidate", "source": "current"},
        ],
        "model": {
            "provider": values.get("TRACEH_PROVIDER", "openai-compatible"),
            "model": MODEL,
            "base_url": base_url,
            "api_key_env": values.get("TRACEH_API_KEY_ENV", "DASHSCOPE_API_KEY"),
            "script": None,
            "retry_policy": {
                "max_attempts": 1,
                "max_elapsed_seconds": 0,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
                "retry_after_cap_seconds": 0,
                "jitter_ratio": 0,
            },
        },
        "execution": {
            "sandbox_config": "../sandbox.json",
            "max_trials": 2 * REPETITIONS,
            "timeout_seconds": EVAL_TIMEOUT_SECONDS,
            "shutdown_seconds": 60,
            "network_mode": "direct",
            "first_arm": "baseline",
        },
        "trials": {"repetitions": REPETITIONS},
        "comparison": {
            "format": 3,
            "kind": "execution_strategy",
            "requested_modes": ["single", "multi"],
            "min_pass_gain": None,
            "max_token_ratio": None,
            "max_tool_call_delta": None,
        },
    }
    write(output / "material/run-plan.json", plan)
    (output / "comparison_driver.py").write_bytes(Path(__file__).read_bytes())
    contract_path = output / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        kind="execution-strategy-paired-comparison",
        case_id=CASE,
        arms=["single", "multi"],
        repetitions=REPETITIONS,
        model=MODEL,
        env_file=str(env_file),
        connection_digest=digest_bytes(base_url.encode("utf-8")),
        authorized_assistants=AUTHORIZED,
        assistant_count_named_in_requirement=False,
        eval_timeout_seconds=EVAL_TIMEOUT_SECONDS,
        budgets={
            "task_tokens": TASK_TOKENS,
            "coder_tokens": CODER_TOKENS,
            "assistant_tokens_each": ASSISTANT_TOKENS,
            "retained_main_tokens": RETAINED_TOKENS,
        },
        interpretation=(
            "Both arms run the same frozen task, checks, image and ceilings; only the "
            "requested execution strategy differs. One replicate per arm is an observation, "
            "not a statistic, and the original comparison owner decides the verdict."
        ),
    )
    contract["files"] = {
        p.relative_to(output).as_posix(): digest_bytes(p.read_bytes())
        for p in output.rglob("*")
        if p.is_file() and p != contract_path
    }
    write(contract_path, contract)


def preflight(output):
    _select()
    if (output / "comparison_driver.py").read_bytes() != Path(__file__).read_bytes():
        raise ValueError("comparison-driver-drift")
    driver.preflight(output)


async def run(output, env_file):
    from traceh.cli.main import _configure_from_environment, _eval, build_parser

    preflight_data = json.loads((output / "preflight.json").read_text(encoding="utf-8"))
    if preflight_data["contract_digest"] != digest_bytes((output / "contract.json").read_bytes()):
        raise ValueError("comparison-preflight-drift")
    with (output / "started.json").open("x", encoding="utf-8") as stream:
        stream.write('{"started":true}\n')
    args = build_parser().parse_args(
        [
            "eval",
            str(output / "material"),
            "--run-plan",
            str(output / "material/run-plan.json"),
            "--output",
            str(output / "experiment"),
            "--env-file",
            str(env_file),
        ]
    )
    _configure_from_environment(args)
    code = await _eval(args)
    write(output / "execution.json", {"exit_code": code})
    print(json.dumps({"exit_code": code}))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "prepare":
        prepare(
            args.repository.resolve(),
            args.sandbox.resolve(),
            output,
            args.env_file.resolve(),
        )
    elif args.action == "preflight":
        preflight(output)
    else:
        raise SystemExit(asyncio.run(run(output, args.env_file.resolve())))
