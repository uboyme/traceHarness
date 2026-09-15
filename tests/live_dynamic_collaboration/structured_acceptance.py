"""One frozen WC-1C episode through the existing ProductTaskEvaluator."""

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
from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file

REQUIREMENT = (
    "实现 replenishment.py 中的 recommend(stock, target, pack, blocked=False) 补货函数。"
    "请明确委派一个只读助手检查 rules.md 的规则和边界并给出文件行证据；"
    "主方拿到报告后核对并实现函数，不要把编码交给助手。只修改 replenishment.py，"
    "保留接口，不改规则，不新增文件，不安装依赖。最终说明实现和验证结果，"
    "没有运行的检查不要声称已运行。"
)
RULES = (
    "# Replenishment rules\n"
    "stock and target are non-negative integers; pack is a positive integer.\n"
    "A negative stock or target, or a non-positive pack, raises ValueError.\n"
    "Validate those limits before applying the blocked flag.\n"
    "Blocked products get zero replenishment.\n"
    "When stock already reaches target, return zero.\n"
    "Otherwise order the smallest whole number of packs that reaches or exceeds target.\n"
    "Return the ordered unit count, not the number of packs.\n"
)
REFERENCE = (
    "def recommend(stock, target, pack, blocked=False):\n"
    "    if stock < 0 or target < 0 or pack <= 0:\n"
    "        raise ValueError('invalid limits')\n"
    "    if blocked or stock >= target:\n"
    "        return 0\n"
    "    return ((target - stock + pack - 1) // pack) * pack\n"
)


class BoundedProvider:
    def __init__(self, inner, maximum=16):
        self.inner, self.name, self.maximum = inner, inner.name, maximum
        self.calls, self.stopped = 0, False
        self.roles = Counter()

    async def complete(self, request):
        if self.stopped or self.calls >= self.maximum:
            self.stopped = True
            raise RuntimeError("wc1c-call-limit-or-prior-failure")
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
    (initial / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (initial / "rules.md").write_text(RULES, encoding="utf-8")
    (initial / "INDEX.md").write_text(
        "# Task files\nrules.md: authoritative replenishment rules\n"
        "replenishment.py: implementation target\n",
        encoding="utf-8",
    )
    (initial / "replenishment.py").write_text(
        "def recommend(stock, target, pack, blocked=False):\n    return None\n", encoding="utf-8"
    )
    fixed = {
        p.name: digest_bytes(p.read_bytes())
        for p in initial.iterdir()
        if p.name != "replenishment.py"
    }
    verifier = (
        "import hashlib\nfrom pathlib import Path\nfrom replenishment import recommend\n"
        f"fixed={fixed!r}\n"
        "assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in fixed.items())\n"
        "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
        "and not {'.git','__pycache__'}&set(p.parts)}\n"
        "assert files==set(fixed)|{'replenishment.py'},files\n"
        "for args,want in [((2,9,4),8),((10,10,4),0),((12,9,4),0),((0,9,4),12),"
        "((1,7,4,True),0),((0,0,3),0),((3,5,1),2)]:\n"
        " assert recommend(*args)==want,(args,want)\n"
        "for args in [(-1,8,2),(1,-8,2),(1,8,0),(1,8,-2,True)]:\n"
        " try: recommend(*args)\n"
        " except ValueError: pass\n"
        " else: raise AssertionError(('expected ValueError',args))\n"
        "print('11 functional cases and source boundaries passed')\n"
    )
    settings = _settings(repository, "wc1c-replenishment")
    settings["roles"]["coder"]["budget"]["max_processes"] = 2
    verification = {
        "plan_id": "wc1c-replenishment",
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
            "policy_id": "wc1c-no-dependencies",
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
                    "case_id": "replenishment",
                    "group_id": "replenishment",
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
            "benchmark_id": "wc1c-structured-collaboration",
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
        raise ValueError("wc1c-source-drift")
    if data["driver_digest"] != digest_bytes(Path(__file__).read_bytes()):
        raise ValueError("wc1c-driver-drift")
    for p, digest in data["files"].items():
        if digest_bytes((output / p).read_bytes()) != digest:
            raise ValueError("wc1c-material-drift")
    return data


def preflight(output):
    validate(output)
    sandbox = load_sandbox_file(output / "sandbox.json")
    assert sandbox.policy.network == "none" and not sandbox.plugin_grants
    case = read_input(output / "material", "dataset.json").data["cases"][0]
    outcomes = []
    for repaired in (False, True):
        with TemporaryDirectory(prefix="traceh-wc1c-preflight-") as directory:
            work = Path(directory).resolve()
            shutil.copytree(output / "material/initial", work, dirs_exist_ok=True)
            if repaired:
                (work / "replenishment.py").write_text(REFERENCE, encoding="utf-8")
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
                raise ValueError("wc1c-preflight-failed")
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
        raise ValueError("wc1c-preflight-drift")
    with (output / "started.json").open("x", encoding="utf-8") as stream:
        stream.write('{"started":true}\n')
    provider = None
    error = None
    try:
        raise RuntimeError(
            "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
        )
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
