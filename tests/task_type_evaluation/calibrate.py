"""Calibrate and admit task-type materials through the production sandbox (plan S0-E).

``calibrate`` runs every hidden test file against the injected initial tree and the
reference tree with no expectations, then freezes: fail-to-pass = tests failing on
the initial tree and passing on the reference; pass-to-pass = tests passing on both.
The reference must pass every hidden test it runs, and every case must have at
least one fail-to-pass test, or calibration is refused.

``admit`` runs each frozen dataset verification plan unchanged: the initial tree
must fail with every fail-to-pass test failing, the reference must pass. It is a
material admission, not an Agent score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from real_repository_evaluation.materials import write_json

from task_type_evaluation.build import spec_digest, verifier
from task_type_evaluation.spec import CASES
from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.sandbox.config import load_sandbox_file
from traceh.sandbox.service import GUEST_ENVIRONMENT, SandboxExecutionService
from traceh.session.sqlite import SqliteEventStore


async def _run(service, workspace, plan, identity, output):
    environment = dict(GUEST_ENVIRONMENT)
    environment.update(plan["environment"]["overrides"])
    command = plan["commands"][0]
    async with service.scope(
        SandboxOwner("verification", identity),
        stream_id="task-type:" + identity,
        workspace=workspace,
        data_dir=None,
        publish_changes=False,
        environment=tuple(sorted(environment.items())),
        retain_output=True,
    ) as scope:
        result = await scope.run(
            tuple(command["argv"]),
            timeout_seconds=command["timeout_ms"] / 1000,
            max_output_bytes=plan["max_output_bytes"],
        )
    (output / (identity + ".stdout")).write_bytes(result.stdout)
    (output / (identity + ".stderr")).write_bytes(result.stderr)
    reports = [
        line.removeprefix("TRACEH_RR_RESULT=")
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.startswith("TRACEH_RR_RESULT=")
    ]
    report = json.loads(reports[0]) if len(reports) == 1 else None
    return result, report


def _payload(plan):
    import base64
    import zlib

    packed = "".join(plan["commands"][0]["argv"][5:])
    return json.loads(zlib.decompress(base64.b64decode(packed)))


def _service(sandbox_file, output):
    store = SqliteEventStore(output / "events")
    policy = load_sandbox_file(sandbox_file).policy
    return (
        store,
        policy,
        SandboxExecutionService(store=store, cas=LocalArtifactCas(output / "cas"), policy=policy),
    )


def _tests(host):
    root = host / "tests"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def calibrate(prep, sandbox_file, output, destination):
    output.mkdir(parents=True, exist_ok=False)
    store, policy, service = _service(sandbox_file, output)
    frozen = {"format": 1, "spec_digest": spec_digest(), "image": policy.image, "cases": {}}
    try:
        for case in CASES:
            name = case["case_id"]
            host = prep / "host" / name
            initial = prep / "material" / name / "initial"
            probe = verifier(case, initial, _tests(host), [], [])
            reports = {}
            for variant, tree in (("initial", initial), ("reference", host / "reference")):
                result, report = await _run(service, tree, probe, f"{name}-{variant}", output)
                if result.status != "finished" or report is None:
                    raise RuntimeError(f"calibration did not finish: {name}-{variant}")
                reports[variant] = report
            ran = set().union(*(set(reports["reference"][k]) for k in ("passed", "failed")))
            broken = set(reports["reference"]["failed"]) | set(reports["reference"]["errors"])
            if broken:
                raise RuntimeError(f"reference does not pass its own tests: {name}")
            before = set(reports["initial"]["passed"])
            fail_to_pass = sorted(ran - before)
            pass_to_pass = sorted(ran & before)
            if not fail_to_pass:
                raise RuntimeError(f"no fail-to-pass test: {name}")
            frozen["cases"][name] = {
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "initial_counts": {
                    k: len(v) for k, v in reports["initial"].items() if isinstance(v, list)
                },
                "reference_counts": {
                    k: len(v) for k, v in reports["reference"].items() if isinstance(v, list)
                },
            }
            print(name, "f2p", len(fail_to_pass), "p2p", len(pass_to_pass), flush=True)
    finally:
        await store.aclose()
    write_json(destination, frozen)
    return frozen


async def admit(built, sandbox_file, output):
    output.mkdir(parents=True, exist_ok=False)
    store, policy, service = _service(sandbox_file, output)
    dataset = json.loads((built / "material" / "dataset.json").read_bytes())
    results = []
    try:
        for row in dataset["cases"]:
            name = row["case_id"]
            plan = row["verification"]
            payload = _payload(plan)
            fail_to_pass = set(payload["fail_to_pass"])
            pass_to_pass = set(payload["pass_to_pass"])
            for variant, tree in (
                ("baseline", built / "material" / name / "initial"),
                ("reference", built / "host" / name / "reference"),
            ):
                result, report = await _run(service, tree, plan, f"{name}-{variant}", output)
                admitted = bool(
                    result.status == "finished"
                    and report is not None
                    and not report["errors"]
                    and not report["missing"]
                    and pass_to_pass <= set(report["passed"])
                    and (
                        fail_to_pass <= set(report["failed"]) and result.exit_code == 1
                        if variant == "baseline"
                        else fail_to_pass <= set(report["passed"]) and result.exit_code == 0
                    )
                )
                results.append(
                    dict(
                        case_id=name,
                        variant=variant,
                        status=result.status,
                        exit_code=result.exit_code,
                        admitted=admitted,
                        tests=report,
                        sandbox_receipt=result.receipt,
                    )
                )
                print(
                    name, variant, result.status, result.exit_code, "admitted", admitted, flush=True
                )
    finally:
        await store.aclose()
    verdict = dict(
        kind="material-admission-not-agent-score",
        image=policy.image,
        complete=len(results) == len(dataset["cases"]) * 2,
        admitted=all(r["admitted"] for r in results),
        results=results,
    )
    write_json(output / "admission.json", verdict)
    return verdict["admitted"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("calibrate")
    one.add_argument("--prep", type=Path, required=True)
    one.add_argument("--destination", type=Path, required=True)
    two = commands.add_parser("admit")
    two.add_argument("--built", type=Path, required=True)
    for sub in (one, two):
        sub.add_argument("--sandbox", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.command == "calibrate":
        asyncio.run(
            calibrate(
                options.prep.resolve(),
                options.sandbox.resolve(),
                options.output.resolve(),
                options.destination.resolve(),
            )
        )
    else:
        ok = asyncio.run(
            admit(options.built.resolve(), options.sandbox.resolve(), options.output.resolve())
        )
        raise SystemExit(0 if ok else 1)
