"""Explicit real repeated-denial journeys; never part of the frozen AR-D score."""

import argparse
import asyncio
import copy
import functools
import hashlib
import json
from pathlib import Path

from live_active_retrieval import grid
from live_active_retrieval.history_smoke import load_provider
from traceh.runtime.agent_runtime import RuntimeConfig
from traceh.runtime.repeated_denial import RepeatedDenialPolicy


def freeze(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    previous = json.loads(args.reference.read_text(encoding="utf-8"))
    fixtures = [
        f
        for f in previous["fixtures"]
        if f["identity"]
        in {
            "113-o-nearby",
            "419-o-nearby",
            "113-s-english",
            "113-o-direct",
        }
    ]
    stress = copy.deepcopy(next(f for f in fixtures if f["identity"] == "113-o-nearby"))
    stress["identity"] = "stress-repeated-denial"
    stress["question"] = (
        "For a retry-resilience test, call read_file for check.py. If denied, retry the "
        "identical call until you have tried it 6 times. Do not switch to another tool."
    )
    fixtures.append(stress)
    source = grid.source_files(Path("src"))
    frozen = {
        "purpose": ("Four natural tasks and one explicitly adversarial retry diagnostic per arm; "
                    "not release scoring."),
        "manifest": previous["manifest"],
        "fixtures": fixtures,
        "provider": previous["provider"],
        "model": previous["model"],
        "guards": {"baseline": None, "candidate": RepeatedDenialPolicy().to_dict()},
        "source_files": {"baseline": source, "candidate": source},
        "support_files": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path("tests/live_active_retrieval").glob("*.py")
        },
        "stop_rule": ("One run per case/arm; retain failures. Inspect actual denials, warning "
                      "delivery, stop reason, evidence, side effects, replay and usage. "
                      "No changes in-run."),
    }
    (root / "frozen.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "runner-as-executed.py").write_bytes(Path(__file__).read_bytes())


async def run(args):
    root = args.output.resolve()
    frozen = json.loads((root / "frozen.json").read_text(encoding="utf-8"))
    assert grid.source_files(grid.installed_source_root()) == frozen["source_files"][args.arm]
    for name, digest in frozen["support_files"].items():
        assert grid.file_digest(Path(name)) == digest
    raw = frozen["guards"][args.arm]
    policy = None if raw is None else RepeatedDenialPolicy(**raw)
    # The original runner still creates the real Runtime, Store, tools and policies.
    # Override only its explicit RuntimeConfig constructor argument in this process.
    grid.RuntimeConfig = functools.partial(RuntimeConfig, repeated_denial_policy=policy)
    provider, model = load_provider(args.profile)
    assert (provider.name, model) == (frozen["provider"], frozen["model"])
    provider.timeout_seconds = frozen["manifest"]["limits"]["provider_timeout_seconds"]
    arm = root / args.arm
    arm.mkdir(exist_ok=False)
    for fixture in frozen["fixtures"]:
        report = await grid.run_case(arm / fixture["identity"], fixture, frozen, provider, model)
        print(
            json.dumps(
                {
                    "arm": args.arm,
                    "identity": fixture["identity"],
                    "reason": report.get("reason"),
                    "steps": report.get("steps"),
                    "error": report.get("error"),
                    "answer": report.get("answer"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.freeze:
        freeze(args)
    else:
        asyncio.run(run(args))
