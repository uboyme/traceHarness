"""Decomposition-granularity probe: the host authorizes four, the model chooses.

Same frozen machinery as the two-assistant acceptance; only the material and the
ceilings differ. The requirement never names a count, so what this measures is
how many assignments the model creates for work that holds four independently
specified units. Multi mode still requires an allocation, so this is not evidence
about choosing whether to collaborate.
"""

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path

from live_dynamic_collaboration import multi_child_acceptance as base
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

CASE = "autonomy-telemetry-pipeline"
# The host authorizes four assistants; the plan may use one to four.
AUTHORIZED = 4
# The first probe answered the granularity question but the main ran out of
# tokens after integrating four Patches: its own context carries every read page,
# every integration receipt and its self-tests. Only this ceiling is raised.
TASK_TOKENS = 2_000_000
CODER_TOKENS = 1_600_000
ASSISTANT_TOKENS = 50_000
RETAINED_TOKENS = 40_000
MAIN_TURN_WALL_MS = 600_000
ASSISTANT_WALL_MS = 240_000
CODER_WALL_MS = MAIN_TURN_WALL_MS + AUTHORIZED * ASSISTANT_WALL_MS + 180_000
ASSISTANT_STEPS = 12
ASSISTANT_TOOL_CALLS = 16
CODER_STEPS = 52 + AUTHORIZED * ASSISTANT_STEPS
CODER_TOOL_CALLS = 72 + AUTHORIZED * ASSISTANT_TOOL_CALLS
CODER_PROCESSES = AUTHORIZED
TASK_PROCESSES = 1 + AUTHORIZED
MAX_REAL_CALLS = 60
TIMEOUT_SECONDS = 900


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
    argv = case["verification"]["commands"][0]["argv"]
    head, marker, tail = argv[3].partition("assert files==set(fixed)|")
    if not marker or not tail:
        raise ValueError("autonomy-verifier-shape-unexpected")
    case["verification"]["commands"][0]["argv"] = [
        *argv[:3],
        head + marker + repr(set(EDITABLE)) + ",files\n" + CHECKS,
    ]
    write(dataset_path, dataset)
    manifest_path = output / "material/benchmark.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = manifest["task_settings"]
    settings["retained_tokens"] = RETAINED_TOKENS
    budget = settings["task_budget"]
    budget.update(
        max_tokens=TASK_TOKENS,
        max_children=AUTHORIZED,
        max_wall_milliseconds=CODER_WALL_MS + 180_000,
        max_steps=CODER_STEPS + 24,
        max_tool_calls=CODER_TOOL_CALLS + 24,
        max_processes=TASK_PROCESSES,
    )
    coder = settings["roles"]["coder"]
    coder["budget"].update(
        max_tokens=CODER_TOKENS,
        max_children=AUTHORIZED,
        max_wall_milliseconds=CODER_WALL_MS,
        max_steps=CODER_STEPS,
        max_tool_calls=CODER_TOOL_CALLS,
        max_processes=CODER_PROCESSES,
    )
    coder["max_turn_wall_milliseconds"] = MAIN_TURN_WALL_MS
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
    for name in ("autonomy_probe.py", "autonomy_materials.py"):
        (output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    contract_path = output / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        kind="autonomy-decomposition-probe",
        case_id=CASE,
        authorized_assistants=AUTHORIZED,
        assistant_count_named_in_requirement=False,
        independently_specified_units=4,
        max_real_calls=MAX_REAL_CALLS,
        timeout_seconds=TIMEOUT_SECONDS,
        budgets={
            "task_tokens": TASK_TOKENS,
            "coder_tokens": CODER_TOKENS,
            "assistant_tokens_each": ASSISTANT_TOKENS,
            "retained_main_tokens": RETAINED_TOKENS,
            "coder_max_children": AUTHORIZED,
            "coder_wall_ms": CODER_WALL_MS,
            "main_turn_wall_ms": MAIN_TURN_WALL_MS,
            "assistant_wall_ms_each": ASSISTANT_WALL_MS,
            "task_processes": TASK_PROCESSES,
            "coder_processes": CODER_PROCESSES,
        },
        measures=(
            "how many assignments the model creates, which files each carries, and which "
            "handoff shape it picks"
        ),
        interpretation=(
            "Multi mode still requires an allocation, so this measures decomposition "
            "granularity, not whether the model would collaborate at all. One run is an "
            "observation, not a rate."
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
    for name in ("autonomy_probe.py", "autonomy_materials.py"):
        if (output / name).read_bytes() != Path(__file__).with_name(name).read_bytes():
            raise ValueError("autonomy-driver-drift")
    driver.preflight(output)


async def run(profile, output):
    _select()
    for name in ("autonomy_probe.py", "autonomy_materials.py"):
        if (output / name).read_bytes() != Path(__file__).with_name(name).read_bytes():
            raise ValueError("autonomy-driver-drift")
    await base.run(profile, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "prepare":
        prepare(args.repository.resolve(), args.sandbox.resolve(), output)
    elif args.action == "preflight":
        preflight(output)
    else:
        asyncio.run(run(args.profile.resolve(), output))
