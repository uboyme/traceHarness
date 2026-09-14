"""Frozen material and offline preflight for the two-assistant acceptance.

Reuses the WC-4 driver's original EvaluationRunner-shaped material layout so the
real terminal driver can consume it unchanged. Budgets are stated per role, never
multiplied by the assistant count.
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.materials import write
from live_dynamic_collaboration.multi_child_materials import (
    CHECKS,
    EDITABLE,
    FILES,
    REFERENCE,
    REQUIREMENT,
)
from traceh.evaluation.inputs import digest_bytes

CASE = "multi-child-telemetry"
ASSISTANTS = 2
# Explicit fixture budgets for one main plus two writable assistants. Each
# assistant's allowance is carved out of the main's by the original ledger, so
# these are stated per role instead of multiplying one number by the count.
TASK_TOKENS = 600_000
CODER_TOKENS = 460_000
ASSISTANT_TOKENS = 60_000
RETAINED_TOKENS = 40_000
# A child reservation carves out its whole allowance, and the main's own Turn
# already holds MAIN_TURN_WALL_MS of the same ceiling. The first attempt failed
# here: 600s main turn + 2x300s assistants exceeded a 720s coder ceiling, so the
# clock is now stated to cover both. Steps/tool calls are sized the same way.
MAIN_TURN_WALL_MS = 600_000
ASSISTANT_WALL_MS = 300_000
CODER_WALL_MS = MAIN_TURN_WALL_MS + ASSISTANTS * ASSISTANT_WALL_MS + 120_000
ASSISTANT_STEPS = 14
ASSISTANT_TOOL_CALLS = 18
CODER_STEPS = 44 + ASSISTANTS * ASSISTANT_STEPS
CODER_TOOL_CALLS = 60 + ASSISTANTS * ASSISTANT_TOOL_CALLS
# Process slots are leased against every ancestor, so the task root must cover
# the main plus every assistant, and the main must cover its assistants. The
# second attempt failed here: a root ceiling of 2 refused the second assistant.
CODER_PROCESSES = ASSISTANTS
TASK_PROCESSES = 1 + ASSISTANTS


def _select():
    driver.FILES, driver.REFERENCE = FILES, REFERENCE
    driver.CHECKS, driver.REQUIREMENT = CHECKS, REQUIREMENT
    driver.EDITABLE = EDITABLE


def prepare(repository, sandbox, output):
    _select()
    driver.prepare(repository, sandbox, output)
    dataset_path = output / "material/dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    case = dataset["cases"][0]
    case.update(case_id=CASE, group_id=CASE)
    # The shared driver freezes its own two-file expectation into the verifier;
    # this round owns three editable files, so the file-set assertion is rebuilt.
    argv = case["verification"]["commands"][0]["argv"]
    head, marker, tail = argv[3].partition("assert files==set(fixed)|")
    if not marker or not tail:
        raise ValueError("multi-child-verifier-shape-unexpected")
    case["verification"]["commands"][0]["argv"] = [
        *argv[:3],
        head + marker + repr(set(EDITABLE)) + ",files\n" + CHECKS,
    ]
    write(dataset_path, dataset)
    manifest_path = output / "material/benchmark.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = manifest["task_settings"]
    settings["retained_tokens"] = RETAINED_TOKENS
    settings["task_budget"]["max_tokens"] = TASK_TOKENS
    settings["task_budget"]["max_children"] = ASSISTANTS
    settings["task_budget"]["max_wall_milliseconds"] = CODER_WALL_MS + 120_000
    settings["task_budget"]["max_steps"] = CODER_STEPS + 20
    settings["task_budget"]["max_tool_calls"] = CODER_TOOL_CALLS + 20
    settings["task_budget"]["max_processes"] = TASK_PROCESSES
    coder = settings["roles"]["coder"]
    coder["budget"]["max_tokens"] = CODER_TOKENS
    # The main's direct-child ceiling is the assistant-count authorization.
    coder["budget"]["max_children"] = ASSISTANTS
    coder["budget"]["max_wall_milliseconds"] = CODER_WALL_MS
    coder["budget"]["max_steps"] = CODER_STEPS
    coder["budget"]["max_tool_calls"] = CODER_TOOL_CALLS
    coder["budget"]["max_processes"] = CODER_PROCESSES
    coder["max_turn_wall_milliseconds"] = MAIN_TURN_WALL_MS
    patch_author = settings["roles"]["patch_author"]
    patch_author["budget"] = dict(
        deepcopy(patch_author["budget"]),
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
    for name in ("multi_child_acceptance.py", "multi_child_materials.py"):
        (output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    contract_path = output / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        kind="multi-child-real-acceptance",
        case_id=CASE,
        assistants=ASSISTANTS,
        assistants_required_by_requirement=True,
        budgets={
            "task_tokens": TASK_TOKENS,
            "coder_tokens": CODER_TOKENS,
            "assistant_tokens_each": ASSISTANT_TOKENS,
            "retained_main_tokens": RETAINED_TOKENS,
            "coder_max_children": ASSISTANTS,
            "coder_wall_ms": CODER_WALL_MS,
            "main_turn_wall_ms": MAIN_TURN_WALL_MS,
            "assistant_wall_ms_each": ASSISTANT_WALL_MS,
            "task_processes": TASK_PROCESSES,
            "coder_processes": CODER_PROCESSES,
        },
        max_real_calls=60,
        timeout_seconds=900,
        interpretation=(
            "The requirement names the assistant count, so this measures the mechanism "
            "and delivery, never a model's spontaneous choice or any speedup."
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
    for name in ("multi_child_acceptance.py", "multi_child_materials.py"):
        if (output / name).read_bytes() != Path(__file__).with_name(name).read_bytes():
            raise ValueError("multi-child-driver-drift")
    driver.preflight(output)


class TimedProvider(driver.BoundedProvider):
    """Record each real call's role and interval, to observe actual overlap."""

    def __init__(self, inner, maximum, calls):
        super().__init__(inner, maximum)
        self.timeline = calls

    async def complete(self, request):
        role = "child" if "traceh.product.patch-author" in request.system_prompt else "main"
        entry = {"index": self.calls + 1, "role": role, "start": time.monotonic(), "end": None}
        self.timeline.append(entry)
        try:
            return await super().complete(request)
        finally:
            entry["end"] = time.monotonic()


def overlap_seconds(calls):
    """Seconds during which a main and at least one child call were in flight."""
    points = sorted({value for call in calls for value in (call["start"], call["end"]) if value})
    total = 0.0
    for left, right in zip(points, points[1:], strict=False):
        roles = {
            call["role"]
            for call in calls
            if call["end"] and call["start"] <= left and call["end"] >= right
        }
        if {"main", "child"} <= roles:
            total += right - left
    return total


async def run(profile, output):
    """One bounded real run with this round's own caps; original owners only."""
    from live_unified_evaluation.baseline import connection

    from traceh.api.json_types import fingerprint
    from traceh.evaluation.plan import RunOptions
    from traceh.evaluation.runner import EvaluationRunner
    from traceh.llm.retry import NO_MODEL_RETRY
    from traceh.sandbox.config import load_sandbox_file

    data = driver.validate(output)
    pre = json.loads((output / "preflight.json").read_text(encoding="utf-8"))
    if pre["contract_digest"] != driver.digest_bytes((output / "contract.json").read_bytes()):
        raise ValueError("multi-child-preflight-drift")
    with (output / "started.json").open("x", encoding="utf-8") as stream:
        stream.write('{"started":true}\n')
    calls: list[dict] = []
    provider = None
    error = None
    started = time.monotonic()
    try:
        args, inner, model = connection(profile)
        provider = TimedProvider(inner, data["max_real_calls"], calls)
        binding = {
            "connection_digest": fingerprint(args.base_url),
            "network_mode": "direct",
            "timeout_seconds": data["connection_timeout_seconds"],
            "call_limit": data["max_real_calls"],
        }
        write(output / "connection.json", {"provider": inner.name, "model": model, **binding})
        sandbox = load_sandbox_file(output / "sandbox.json")
        runner = EvaluationRunner(
            output / "material",
            output / "run",
            provider=provider,
            model_id=model,
            retry_policy=NO_MODEL_RETRY,
            sandbox=sandbox.policy,
            options=RunOptions(
                repetitions=1, max_trials=1, timeout_seconds=data["timeout_seconds"]
            ),
            provider_binding=binding,
        )
        await runner.run()
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        roles = Counter(call["role"] for call in calls)
        write(
            output / "execution.json",
            {
                "real_calls": provider.calls if provider else 0,
                "roles": dict(roles),
                "error_type": error,
                "stopped": provider.stopped if provider else True,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "observed_main_child_overlap_seconds": round(overlap_seconds(calls), 3),
                "calls": [
                    {
                        "index": call["index"],
                        "role": call["role"],
                        "seconds": None
                        if call["end"] is None
                        else round(call["end"] - call["start"], 3),
                    }
                    for call in calls
                ],
                "measurement": (
                    "In-flight provider call intervals on one host clock; overlap is "
                    "concurrency of model calls, not a speedup claim."
                ),
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--sandbox", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "prepare":
        prepare(args.repository.resolve(), args.sandbox.resolve(), output)
    elif args.action == "preflight":
        preflight(output)
    else:
        asyncio.run(run(args.profile.resolve(), output))
