"""Explicit small paired experiment; never counted as the frozen AR-D grid."""

import argparse
import asyncio
import copy
import json
from pathlib import Path

from live_active_retrieval.fixtures import materialize
from live_active_retrieval.grid import installed_source_root, run_case, source_files, write
from live_active_retrieval.history_smoke import load_provider


async def main(args):
    root = args.output.resolve()
    frozen = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    assert source_files(installed_source_root()) == frozen["source_files"][args.arm]
    provider, model = load_provider(args.profile)
    assert (provider.name, model) == (frozen["provider"], frozen["model"])
    provider.timeout_seconds = frozen["manifest"]["limits"]["provider_timeout_seconds"]
    arm = root / args.arm
    arm.mkdir(exist_ok=False)
    for fixture in frozen["fixtures"]:
        report = await run_case(arm / fixture["identity"], fixture, frozen, provider, model)
        print(json.dumps({"arm": args.arm, "case": fixture["identity"],
                          "answer": report.get("answer"), "error": report.get("error"),
                          "provisional": report["provisional_joint_pass"]},
                         ensure_ascii=False), flush=True)


def freeze(args):
    previous = json.loads(args.reference.read_text(encoding="utf-8"))
    manifest = copy.deepcopy(previous["manifest"])
    manifest["repeat_seeds"] = [113, 887]
    manifest["cases"] = [c for c in manifest["cases"] if c["family"] == "output"]
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output / "experiment.json", {
        "purpose": "One presentation-only change; 6 known and 6 new-value cases per arm. "
                   "Same questions, original limits, same model. "
                   "Diagnostic, not release acceptance.",
        "hypothesis": "Explicit metadata/content semantics reduce field misattribution.",
        "stop_rule": "Run each case once per arm, retain all failures; no prompt tuning mid-run. "
                     "Do not claim independent held-out questions or model-strength attribution.",
        "manifest": manifest, "fixtures": materialize(manifest),
        "provider": previous["provider"], "model": previous["model"],
        "source_files": {"baseline": source_files(args.baseline_source),
                         "candidate": source_files(Path("src"))},
    }, exclusive=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--baseline-source", type=Path)
    args = parser.parse_args()
    if args.freeze:
        freeze(args)
    else:
        asyncio.run(main(args))
