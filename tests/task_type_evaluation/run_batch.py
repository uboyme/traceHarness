"""Run one frozen batch of paired plans sequentially through the public CLI.

Each plan's bytes must still match the digest recorded when it was generated.
Plans run one after another (never in parallel) with ``traceh eval``; every arm
runs in a private worker that connects directly. After each plan the original
comparison report is read and one ledger line appended. Stop rules (collection
plan section 8): an invariant violation stops the batch at once; two consecutive
plans with a provider failure or unknown usage stop it; nothing is re-run to
replace a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summarize(output):
    execution = json.loads((output / "execution.json").read_bytes())
    report = json.loads((output / "comparison" / "report.json").read_bytes())
    pairs = [
        {
            "case_id": pair["key"]["case_id"],
            "assessment": pair["assessment"],
            "execution": pair["execution"],
            "invariants": pair["invariants"],
            "metrics": pair["metrics"],
        }
        for pair in report["pairs"]
    ]
    unknown = any(
        m is None or m.get("unknown_attempts", 0) > 0 for p in pairs for m in p["metrics"]
    )
    provider = bool(execution["errors"]) or unknown
    violated = any(v == "violated" for p in pairs for v in p["invariants"])
    return {
        "status": report["status"],
        "complete": report["complete"],
        "hard_constraints": report["hard_constraints"],
        "execution_errors": execution["errors"],
        "pairs": pairs,
        "provider_or_unknown": provider,
        "invariant_violated": violated,
    }


def run(manifest, benchmark, output_root, ledger, env_file=None):
    entries = json.loads(Path(manifest).read_bytes())["plans"]
    for entry in entries:
        if _digest(entry["plan"]) != entry["sha256"]:
            raise SystemExit("plan changed after it was frozen: " + entry["plan"])
    environment = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    environment["PYTHONUTF8"] = "1"
    consecutive = 0
    Path(output_root).mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(entries, 1):
        # Short names: Git for Windows worktrees fail on long paths, and deep
        # upstream trees (astroid) under a long output root exceed the limit.
        name = f"p{index:02d}"
        output = Path(output_root) / name
        started = datetime.now(UTC).isoformat()
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "traceh.cli.main",
                "eval",
                str(Path(benchmark).resolve()),
                "--run-plan",
                str(Path(entry["plan"]).resolve()),
                "--output",
                str(output.resolve()),
                *(() if env_file is None else ("--env-file", str(Path(env_file).resolve()))),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=6 * 3600,
        )
        (Path(output_root) / (name + ".stdout")).write_bytes(process.stdout)
        (Path(output_root) / (name + ".stderr")).write_bytes(process.stderr)
        record = {
            "name": name,
            "plan": entry["plan"],
            "plan_sha256": entry["sha256"],
            "output": str(output),
            "started_utc": started,
            "finished_utc": datetime.now(UTC).isoformat(),
            "exit_code": process.returncode,
        }
        try:
            record.update(summarize(output))
        except (OSError, KeyError, ValueError) as error:
            record.update(summary_error=type(error).__name__, provider_or_unknown=True)
        with Path(ledger).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(name, record.get("status"), record.get("execution_errors"), flush=True)
        if record.get("invariant_violated"):
            print("STOP: invariant violated", flush=True)
            return 2
        consecutive = consecutive + 1 if record.get("provider_or_unknown") else 0
        if consecutive >= 2:
            print("STOP: two consecutive provider failures or unknown usage", flush=True)
            return 3
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    options = parser.parse_args()
    raise SystemExit(
        run(options.manifest, options.benchmark, options.output, options.ledger, options.env_file)
    )
