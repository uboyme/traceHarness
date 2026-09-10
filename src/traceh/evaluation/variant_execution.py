"""Own two isolated workers; each runs the same EvaluationRunner trial lifecycle."""

import asyncio
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from uuid import uuid4

from traceh.api.json_types import canonical_json, fingerprint, to_json_value
from traceh.concurrency import await_worker_convergence
from traceh.evaluation.errors import BenchmarkExecutionError, BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.variants import (
    apply_candidate,
    environment_identity,
    source_digest,
    source_files,
)
from traceh.process_control import converge_process


@dataclass(frozen=True)
class ExperimentReport:
    encoded: str

    def to_dict(self):
        return json.loads(self.encoded)

    @property
    def complete(self):
        return self.to_dict()["complete"]


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


async def _stop_worker(process, grace):
    """Cooperate first so Runtime/Sandbox close; forced direct-child exit is unproven."""
    if process.returncode is not None:
        return False
    try:
        process.stdin.write(b"CANCEL\n")
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    try:
        async with asyncio.timeout(grace):
            await process.wait()
        return False
    except TimeoutError:
        await converge_process(process)
        return True


async def run_worker(code, request, *, duration_seconds, shutdown, environment):
    """No shell and no raw child logs. Repeated cancellation waits for the owned cleanup."""
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from traceh.evaluation.worker import main; main(sys.argv[2])",
            str(code),
            str(request),
            cwd=request.parent,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    cancelled = None
    timed_out, forced = False, False
    try:
        process = await asyncio.shield(spawn)
        async with asyncio.timeout(duration_seconds):
            await process.wait()
    except (asyncio.CancelledError, TimeoutError) as error:
        cancelled = error if isinstance(error, asyncio.CancelledError) else None
        timed_out = cancelled is None
        await await_worker_convergence(spawn)
        if not spawn.cancelled() and spawn.exception() is None:
            process = spawn.result()
            stop = asyncio.create_task(_stop_worker(process, shutdown))
            await await_worker_convergence(stop)
            forced = stop.result()
        else:
            if cancelled:
                raise cancelled from None
            raise BenchmarkExecutionError("evaluation-worker-start-failed") from None
    finally:
        if spawn.done() and not spawn.cancelled() and spawn.exception() is None:
            process = spawn.result()
            if process.stdin is not None:
                process.stdin.close()
    outcome = {
        "owner_pid": os.getpid(),
        "pid": process.pid,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "forced_stop": forced,
        "cancelled": cancelled is not None,
    }
    write_json(request.parent / "process.json", outcome)
    if cancelled:
        raise cancelled from None
    return outcome


def _archive(path, files):
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)


async def execute_variants(runner):
    """No second evaluator loop: select, freeze, invoke the existing runner twice."""
    from traceh.evaluation.comparison import compare_experiment

    options = runner.options
    if options.document is None or options != load_run_options(
        options.document.root / options.document.relative
    ):
        raise BenchmarkManifestError("evaluation-run-plan-conflict", "variants")
    runner.evaluator.verify_inputs()
    options.document.verify()
    raw = options.document.data
    root = runner.output
    _, sources = source_files()
    base_digest = source_digest(sources)
    environment = to_json_value(environment_identity())
    materials = runner.evaluator.frozen_materials()
    variants = []
    for v in options.variants:
        if v.patch is not None:
            v.patch.verify()
        files = sources if v.patch is None else apply_candidate(sources, v.patch.data)
        variants.append((v, files))
    # Freeze explicit connection files through the same bounded file loader; never read a key.
    inputs = {}
    for field, group in (("script", "model"), ("sandbox_config", "execution")):
        name = raw[group][field]
        if name is not None:
            path = (options.document.root / name).resolve()
            inputs[field] = read_input(path.parent, path.name)
    root.mkdir(parents=True, exist_ok=False)
    (root / "artifacts").mkdir()
    (root / "materials").mkdir()
    (root / "inputs").mkdir()
    _archive(root / "artifacts/base-source.zip", sources)
    _archive(root / "artifacts/materials.zip", materials)
    for name, data in materials:
        path = root / "materials" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for field, document in inputs.items():
        (root / "inputs" / (field + ".json")).write_bytes(document.content)
    (root / "inputs/empty.env").write_bytes(b"")
    (root / "inputs/run-plan.json").write_bytes(options.document.content)
    arms = []
    for index, (variant, files) in enumerate(variants, 1):
        relative = f"arms/{index:02d}"
        directory = root / relative
        code = directory / "code/traceh"
        code.mkdir(parents=True)
        for name, data in files:
            path = code / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        patch_ref = None
        if variant.patch is not None:
            name = f"inputs/patch-{index:02d}.json"
            (root / name).write_bytes(variant.patch.content)
            patch_ref = {"file": name, "sha256": variant.patch.sha256}
        model = dict(raw["model"])
        model["script"] = None if "script" not in inputs else "../../inputs/script.json"
        execution = {
            k: raw["execution"][k] for k in ("sandbox_config", "max_trials", "timeout_seconds")
        }
        selected = [to_json_value(t) for t in runner.trials if t.variant_id == variant.variant_id]
        execution["max_trials"] = len(selected)
        execution["sandbox_config"] = (
            None if "sandbox_config" not in inputs else "../../inputs/sandbox_config.json"
        )
        plan = {
            **raw,
            "variants": [
                {"variant_id": variant.variant_id, "role": "current", "source": "current"}
            ],
            "model": model,
            "execution": execution,
            "comparison": None,
        }
        write_json(directory / "plan.json", plan)
        arms.append(
            {
                "variant_id": variant.variant_id,
                "role": variant.role,
                "source_digest": source_digest(files),
                "patch": patch_ref,
                "directory": relative,
                "trials": selected,
                "plan_sha256": digest_bytes((directory / "plan.json").read_bytes()),
            }
        )
    frozen = {
        "format": 1,
        "run_id": str(uuid4()),
        "benchmark_id": runner.manifest.benchmark_id,
        "task_type": runner.manifest.task_type.value,
        "base_source_digest": base_digest,
        "environment": environment,
        "benchmark_digest": runner.manifest.document.sha256,
        "settings": runner.evaluator.frozen_settings(),
        "model": raw["model"],
        "provider_implementation": runner.provider_implementation,
        "sandbox": to_json_value(runner.sandbox),
        "execution": raw["execution"],
        "comparison": raw["comparison"],
        "arms": arms,
        "artifacts": [
            {"file": p.relative_to(root).as_posix(), "sha256": digest_bytes(p.read_bytes())}
            for p in sorted((root / "artifacts").iterdir())
        ],
        "inputs": [
            {"file": p.relative_to(root).as_posix(), "sha256": digest_bytes(p.read_bytes())}
            for p in sorted((root / "inputs").iterdir())
        ],
    }
    write_json(root / "experiment.json", frozen)
    started = time.monotonic()
    primary, outcomes = None, []
    # Child environment is private to the process. Never write its values into artifacts.
    child_env = dict(os.environ)
    if runner.worker_api_key is not None:
        child_env[raw["model"]["api_key_env"]] = runner.worker_api_key
    for key in tuple(child_env):
        if key.upper().endswith("_PROXY") or key.upper() in {"PYTHONPATH", "PYTHONHOME"}:
            child_env.pop(key)
    child_env["PYTHONUTF8"] = "1"
    for arm in arms:
        directory = root / arm["directory"]
        request = {
            "format": 1,
            "experiment_digest": fingerprint(frozen),
            "arm": arm,
            "environment": environment,
            "root": str(root),
        }
        write_json(directory / "worker.json", request)
        try:
            runner.evaluator.verify_inputs()
            options.document.verify()
            for doc in inputs.values():
                doc.verify()
            if source_digest(source_files()[1]) != base_digest:
                raise BenchmarkExecutionError("evaluation-frozen-input-drift")
            remaining = options.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise BenchmarkExecutionError("evaluation-deadline")
            outcome = await run_worker(
                directory / "code",
                directory / "worker.json",
                duration_seconds=remaining,
                shutdown=raw["execution"]["shutdown_seconds"],
                environment=child_env,
            )
            outcomes.append(outcome)
            if outcome["exit_code"] not in (0, 4) or outcome["forced_stop"] or outcome["timed_out"]:
                raise BenchmarkExecutionError("evaluation-worker-incomplete")
            receipt_file = read_input(directory, "worker-receipt.json")
            receipt = receipt_file.data
            outcome["receipt_sha256"] = receipt_file.sha256
            if (
                receipt["request_digest"] != fingerprint(request)
                or receipt["source_digest"] != arm["source_digest"]
                or receipt["environment"] != environment
                or receipt["convergence"] != "converged"
            ):
                raise BenchmarkExecutionError("evaluation-worker-incomplete")
            runner.evaluator.verify_inputs()
            options.document.verify()
            for doc in inputs.values():
                doc.verify()
            if source_digest(source_files()[1]) != base_digest:
                raise BenchmarkExecutionError("evaluation-frozen-input-drift")
        except BaseException as error:
            primary = error
            break
    errors = [] if primary is None else [getattr(primary, "code", type(primary).__name__)]
    write_json(
        root / "execution.json",
        {
            "format": 1,
            "experiment_digest": fingerprint(frozen),
            "outcomes": outcomes,
            "errors": errors,
        },
    )
    report = compare_experiment(root, root / "comparison")
    if primary is not None and not isinstance(primary, BenchmarkExecutionError):
        raise primary
    return ExperimentReport(canonical_json(report))
