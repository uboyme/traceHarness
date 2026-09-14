"""One frozen WC-1G episode through the existing ProductTaskEvaluator."""

import argparse
import asyncio
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.decomposition import _settings
from live_dynamic_collaboration.materials import write
from live_dynamic_collaboration.verification_materials import CHECKS, FILES, REFERENCE, REQUIREMENT
from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file


class BoundedProvider:
    def __init__(self, inner, maximum=16):
        self.inner, self.name, self.maximum = inner, inner.name, maximum
        self.calls, self.stopped = 0, False
        self.roles = Counter()

    async def complete(self, request):
        if self.stopped or self.calls >= self.maximum:
            self.stopped = True
            raise RuntimeError("wc1g-call-limit-or-prior-failure")
        self.calls += 1
        child = any(t.name == "request_investigation_budget" for t in request.tools)
        self.roles["child" if child else "main"] += 1
        print(f"Real call {self.calls}/{self.maximum}: {'child' if child else 'main'}", flush=True)
        try:
            return await self.inner.complete(request)
        except BaseException:
            self.stopped = True
            raise


def prepare(repository, sandbox, output):
    output.mkdir(parents=True, exist_ok=False)
    (output / "sandbox.json").write_bytes(sandbox.read_bytes())
    initial = output / "material" / "initial"
    initial.mkdir(parents=True)
    for name, content in FILES.items():
        (initial / name).write_text(content, encoding="utf-8")
    fixed = {
        p.name: digest_bytes(p.read_bytes())
        for p in initial.iterdir()
        if p.name != "coverage_windows.py"
    }
    verifier = (
        "import hashlib\nfrom pathlib import Path\n"
        f"fixed={fixed!r}\n"
        "assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in fixed.items())\n"
        "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
        "and not {'.git','__pycache__'}&set(p.parts)}\n"
        "assert files==set(fixed)|{'coverage_windows.py'},files\n"
    ) + CHECKS
    settings = _settings(repository, "wc1g-coverage_windows")
    settings.update(profile_id="wc1g-multi", default_mode="multi", modes=["multi"])
    settings["roles"]["coder"]["budget"]["max_processes"] = 2
    settings["roles"]["coder"]["max_turn_wall_milliseconds"] = 300_000
    verification = {
        "plan_id": "wc1g-coverage_windows",
        "plan_version": 1,
        "protocol_version": 3,
        "commands": [
            {
                "public_requirement": None,
                "command_id": "functional-contract",
                "argv": ["python", "-B", "-c", verifier],
                "timeout_ms": 30000,
            }
        ],
        "environment": {
            "policy_id": "wc1g-no-dependencies",
            "passthrough": [],
            "overrides": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
        },
        "max_output_bytes": 1048576,
    }
    write(
        output / "material/dataset.json",
        {
            "format": 2,
            "cases": [
                {
                    "case_id": "coverage_windows",
                    "group_id": "coverage_windows",
                    "requirement": REQUIREMENT,
                    "initial_tree": "initial",
                    "sha256": initial_tree_digest(capture_initial_tree(initial)),
                    "verification": verification,
                }
            ],
        },
    )
    write(
        output / "material/benchmark.json",
        {
            "protocol_version": 3,
            "benchmark_id": "wc1g-structured-collaboration",
            "task_type": "product_task",
            "task_settings": settings,
            "dataset": {
                "file": "dataset.json",
                "sha256": digest_bytes((output / "material/dataset.json").read_bytes()),
            },
            "assessment": {
                "scorer_id": "product-durable-v1",
                "version": 1,
                "requires_review": False,
                "rubric": None,
            },
        },
    )
    write(
        output / "contract.json",
        {
            "format": 1,
            "source_digest": source_digest(source_files()[1]),
            "driver_digest": digest_bytes(Path(__file__).read_bytes()),
            "materials_driver_digest": digest_bytes(
                Path(__file__).with_name("verification_materials.py").read_bytes()
            ),
            "files": {
                p.relative_to(output).as_posix(): digest_bytes(p.read_bytes())
                for p in output.rglob("*")
                if p.is_file()
            },
            "max_real_calls": 16,
            "timeout_seconds": 600,
            "connection_timeout_seconds": 60,
            "retry_attempts": 1,
            "trials": 1,
            "network": "direct",
            "promotion": "isolated-benchmark-only",
        },
    )


def validate(output):
    data = read_input(output, "contract.json").data
    if data["source_digest"] != source_digest(source_files()[1]):
        raise ValueError("wc1g-source-drift")
    if data["driver_digest"] != digest_bytes(Path(__file__).read_bytes()):
        raise ValueError("wc1g-driver-drift")
    if data["materials_driver_digest"] != digest_bytes(
        Path(__file__).with_name("verification_materials.py").read_bytes()
    ):
        raise ValueError("wc1g-material-generator-drift")
    for p, digest in data["files"].items():
        if digest_bytes((output / p).read_bytes()) != digest:
            raise ValueError("wc1g-material-drift")
    return data


def preflight(output):
    validate(output)
    sandbox = load_sandbox_file(output / "sandbox.json")
    assert sandbox.policy.network == "none" and not sandbox.plugin_grants
    case = read_input(output / "material", "dataset.json").data["cases"][0]
    outcomes = []
    for repaired in (False, True):
        with TemporaryDirectory(prefix="traceh-wc1g-preflight-") as directory:
            work = Path(directory).resolve()
            shutil.copytree(output / "material/initial", work, dirs_exist_ok=True)
            if repaired:
                (work / "coverage_windows.py").write_text(REFERENCE, encoding="utf-8")
            result = subprocess.run(
                [
                    "docker",
                    "--context",
                    sandbox.policy.docker_context,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--memory",
                    "256m",
                    "--pids-limit",
                    "64",
                    "--cpus",
                    "1",
                    "--mount",
                    f"type=bind,source={work},target=/work,readonly",
                    "--workdir",
                    "/work",
                    sandbox.policy.image,
                    *case["verification"]["commands"][0]["argv"],
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            expected = 0 if repaired else 1
            if result.returncode != expected or (
                not repaired and "AssertionError" not in result.stderr
            ):
                raise ValueError("wc1g-preflight-failed")
            outcomes.append({"reference": repaired, "exit_code": result.returncode})
    write(
        output / "preflight.json",
        {
            "contract_digest": digest_bytes((output / "contract.json").read_bytes()),
            "outcomes": outcomes,
        },
    )


async def run(profile, output):
    data = validate(output)
    pre = read_input(output, "preflight.json").data
    if pre["contract_digest"] != digest_bytes((output / "contract.json").read_bytes()):
        raise ValueError("wc1g-preflight-drift")
    with (output / "started.json").open("x", encoding="utf-8") as stream:
        stream.write('{"started":true}\n')
    provider = None
    error = None
    try:
        args, inner, model = connection(profile)
        provider = BoundedProvider(inner, data["max_real_calls"])
        binding = {
            "connection_digest": fingerprint(args.base_url),
            "network_mode": "direct",
            "timeout_seconds": 60,
            "call_limit": 16,
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
            options=RunOptions(repetitions=1, max_trials=1, timeout_seconds=600),
            provider_binding=binding,
        )
        await runner.run()
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        write(
            output / "execution.json",
            {
                "real_calls": provider.calls if provider else 0,
                "roles": dict(provider.roles) if provider else {},
                "error_type": error,
                "stopped": provider.stopped if provider else True,
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.repository.resolve(), args.sandbox.resolve(), args.output.resolve())
    elif args.action == "preflight":
        preflight(args.output.resolve())
    else:
        asyncio.run(run(args.profile.resolve(), args.output.resolve()))
