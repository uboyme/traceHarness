"""One explicit command-recovery probe; larger fixture budget, original owners."""

import argparse
import asyncio
import json
from pathlib import Path

from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.materials import write
from live_dynamic_collaboration.stability import select
from traceh.evaluation.inputs import digest_bytes


def prepare(repository, sandbox, output):
    select("command-recovery")
    driver.prepare(repository, sandbox, output)
    dataset_path = output / "material/dataset.json"
    dataset = json.loads(dataset_path.read_text())
    dataset["cases"][0].update(case_id="command-recovery", group_id="command-recovery")
    write(dataset_path, dataset)
    manifest_path = output / "material/benchmark.json"
    manifest = json.loads(manifest_path.read_text())
    settings = manifest["task_settings"]
    settings["task_budget"]["max_tokens"] = 480_000
    settings["roles"]["coder"]["budget"]["max_tokens"] = 360_000
    manifest["dataset"]["sha256"] = digest_bytes(dataset_path.read_bytes())
    write(manifest_path, manifest)
    for name in ("closure_acceptance.py", "stability.py", "stability_materials.py"):
        (output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    contract_path = output / "contract.json"
    contract = json.loads(contract_path.read_text())
    contract["files"] = {
        p.relative_to(output).as_posix(): digest_bytes(p.read_bytes())
        for p in output.rglob("*") if p.is_file() and p != contract_path
    }
    write(contract_path, contract)


async def run(profile, output):
    for name in ("closure_acceptance.py", "stability.py", "stability_materials.py"):
        if (output / name).read_bytes() != Path(__file__).with_name(name).read_bytes():
            raise ValueError("closure-driver-drift")
    select("command-recovery")
    await driver.run(profile, output)


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
        select("command-recovery")
        driver.preflight(output)
    else:
        asyncio.run(run(args.profile.resolve(), output))
