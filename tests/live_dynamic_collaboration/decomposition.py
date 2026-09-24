"""Bounded real-model observations for Adaptive task decomposition.

The driver freezes three fixtures before connecting to the provider and reuses
the existing ProductTaskEvaluator path.  It is not a second evaluator, a
semantic judge, a baseline run, or an adoption mechanism.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

from evaluation_fixtures import capture_fixture_tree, fixture_tree_limits
from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.budget_autonomy import CappedProvider
from live_dynamic_collaboration.diagnosis_audit import events_at
from live_dynamic_collaboration.materials import budgets, write
from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.repositories import initial_tree_digest
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file

SCENARIOS = {
    "separable-review": {
        "kind": "separable",
        "max_real_calls": 20,
        "source_paths": (
            "src/traceh/budgets/projection.py",
            "src/traceh/budgets/service.py",
            "src/traceh/supervision/delegation.py",
            "src/traceh/supervision/investigation_budget.py",
        ),
    },
    "coupled-review": {
        "kind": "coupled",
        "max_real_calls": 10,
        "source_paths": ("src/traceh/supervision/investigation_budget.py",),
    },
    "simple-read": {
        "kind": "simple",
        "max_real_calls": 6,
        "source_paths": ("src/traceh/version.py",),
    },
}
EXPECTED_TOPICS = ("allocation", "handoff")


def _requirement(kind: str) -> str:
    if kind == "separable":
        return (
            "评审当前快照中的两组只读设计并交付 answer.json。先读取 INDEX.md。分别核对："
            "第一，额度分配、决策及账本投影如何维持当前状态；第二，只读调查如何创建、交接、"
            "续派并由主方收集报告。两个主题分别使用 allocation 和 handoff，每项写出结论并至少"
            "引用两处实际代码行。输出格式为 "
            '{"sections":[{"topic":"allocation","conclusion":"...",'
            '"evidence":[{"path":"...","start_line":1,"end_line":2}]}]}。'
            "每个主题恰好一次，只新增 answer.json，不安装依赖，不修改原文件，也不要声称运行了"
            "未运行的测试。"
        )
    if kind == "coupled":
        return (
            "读取 src/traceh/supervision/investigation_budget.py，沿同一个 "
            "InvestigationBudgetContinuation.after_tool_results 状态链说明：它先检查什么、何时产生"
            "追加额度请求、何时继续或停止。将一个连贯结论和至少三处实际代码行写入 answer.json，"
            '格式为 {"conclusion":"...","evidence":[{"path":"...",'
            '"start_line":1,"end_line":2}]}。只新增 answer.json。'
        )
    return (
        "读取 src/traceh/version.py，把代码中的当前版本写入 answer.json，格式为"
        ' {"version":"实际版本"}。只新增这一个文件并检查内容。'
    )


def _copy_sources(repository: Path, initial: Path, paths: tuple[str, ...]) -> None:
    for relative in paths:
        target = initial / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repository / relative).read_bytes())


def _verification(initial: Path, paths: tuple[str, ...], kind: str) -> tuple[dict, dict]:
    hashes = {name: digest_bytes((initial / name).read_bytes()) for name in paths}
    prefix = (
        "import hashlib,json\nfrom pathlib import Path\n"
        f"expected={hashes!r}\n"
        "assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h "
        "for n,h in expected.items())\n"
        "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
        "and '.git' not in p.parts}\n"
        "assert files-set(expected)=={'answer.json'}, files\n"
        "assert Path('answer.json').is_file(), 'answer.json missing'\n"
        "answer=json.loads(Path('answer.json').read_text(encoding='utf-8'))\n"
    )
    evidence_check = (
        "for ref in answer['evidence']:\n"
        " assert set(ref)=={'path','start_line','end_line'} and ref['path'] in expected\n"
        " lines=Path(ref['path']).read_text(encoding='utf-8').splitlines()\n"
        " assert type(ref['start_line']) is int and type(ref['end_line']) is int\n"
        " assert 1<=ref['start_line']<=ref['end_line']<=len(lines)\n"
    )
    if kind == "simple":
        source = (initial / paths[0]).read_text(encoding="utf-8")
        version = source.split('__version__ = "', 1)[1].split('"', 1)[0]
        body = prefix + f"assert answer=={{'version':{version!r}}}\n"
        reference = {"version": version}
    elif kind == "coupled":
        body = (
            prefix
            + "assert set(answer)=={'conclusion','evidence'}\n"
            + "assert isinstance(answer['conclusion'],str) and len(answer['conclusion'])>=40\n"
            + "assert isinstance(answer['evidence'],list) and len(answer['evidence'])>=3\n"
            + evidence_check
        )
        reference = {
            "conclusion": (
                "This reference output proves only the frozen structural verifier contract."
            ),
            "evidence": [
                {"path": paths[0], "start_line": 1, "end_line": 1},
                {"path": paths[0], "start_line": 2, "end_line": 2},
                {"path": paths[0], "start_line": 3, "end_line": 3},
            ],
        }
    else:
        section_check = (
            "for section in answer['sections']:\n"
            " assert set(section)=={'topic','conclusion','evidence'}\n"
            " assert isinstance(section['conclusion'],str) and len(section['conclusion'])>=40\n"
            " assert isinstance(section['evidence'],list) and len(section['evidence'])>=2\n"
            " answer={'evidence':section['evidence']}\n" + evidence_check
        )
        body = (
            prefix
            + "assert set(answer)=={'sections'} and len(answer['sections'])==2\n"
            + f"assert {{s['topic'] for s in answer['sections']}}=={set(EXPECTED_TOPICS)!r}\n"
            + section_check
        )
        reference = {
            "sections": [
                {
                    "topic": topic,
                    "conclusion": (
                        "This reference output proves only the frozen structural verifier contract."
                    ),
                    "evidence": [
                        {"path": paths[index * 2], "start_line": 1, "end_line": 1},
                        {"path": paths[index * 2 + 1], "start_line": 1, "end_line": 1},
                    ],
                }
                for index, topic in enumerate(EXPECTED_TOPICS)
            ]
        }
    return (
        {
            "plan_id": "adaptive-decomposition-output-contract",
            "plan_version": 1,
            "protocol_version": 3,
            "commands": [
                {
                    "public_requirement": None,
                    "command_id": "answer-contract",
                    "argv": ["python", "-B", "-c", body],
                    "timeout_ms": 30_000,
                }
            ],
            "environment": {
                "policy_id": "adaptive-decomposition-no-dependencies",
                "passthrough": [],
                "overrides": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
            },
            "max_output_bytes": 1_048_576,
        },
        reference,
    )


def _settings(repository: Path, scenario_id: str) -> dict:
    settings = json.loads(
        (repository / "benchmarks/dynamic_collaboration_v1/development/benchmark.json").read_text(
            encoding="utf-8"
        )
    )["task_settings"]
    settings.update(
        profile_id="adaptive-decomposition-" + scenario_id,
        default_mode="adaptive",
        modes=["adaptive"],
        retained_tokens=40_000,
        investigator_initial_tokens=20_000,
        max_report_chars=12_000,
    )
    settings["task_budget"] = budgets(360_000, 80, 100, 900_000, 2, 2, 2)
    settings["roles"]["coder"].update(
        max_output_tokens=6_000,
        max_turn_wall_milliseconds=180_000,
        budget=budgets(240_000, 44, 60, 720_000, 1, 1, 1),
    )
    settings["roles"]["investigator"].update(
        max_output_tokens=3_072,
        max_turn_wall_milliseconds=120_000,
        budget=budgets(30_000, 14, 18, 240_000, 0, 0, 0),
    )
    return settings


def prepare(repository: Path, sandbox: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "sandbox.json").write_bytes(sandbox.read_bytes())
    contract = {
        "format": 1,
        "kind": "adaptive-decomposition-real-observation",
        "source_digest": source_digest(source_files()[1]),
        "sandbox_digest": digest_bytes(sandbox.read_bytes()),
        "network": "direct",
        "retry_attempts": 1,
        "scenario_order": list(SCENARIOS),
        "scenarios": SCENARIOS,
        "max_total_real_calls": sum(item["max_real_calls"] for item in SCENARIOS.values()),
        "semantic_judge_calls": 0,
        "baseline_trials": 0,
        "adoption_authorized": False,
        "interpretation": "Three bounded observations; no statistical significance claim.",
    }
    for scenario_id, scenario in SCENARIOS.items():
        root = output / "materials" / scenario_id
        initial = root / "initial"
        initial.mkdir(parents=True)
        paths = scenario["source_paths"]
        _copy_sources(repository, initial, paths)
        if scenario["kind"] == "separable":
            (initial / "INDEX.md").write_text(
                "# Source map\n\n" + "\n".join(f"- `{path}`" for path in paths) + "\n",
                encoding="utf-8",
            )
            paths = (*paths, "INDEX.md")
        verification, reference = _verification(initial, paths, scenario["kind"])
        case = {
            "case_id": scenario_id,
            "group_id": scenario_id,
            "requirement": _requirement(scenario["kind"]),
            "initial_tree": "initial",
            "initial_tree_limits": fixture_tree_limits(),
            "sha256": initial_tree_digest(capture_fixture_tree(initial)),
            "verification": verification,
        }
        write(root / "dataset.json", {"format": 3, "cases": [case]})
        manifest = {
            "protocol_version": 3,
            "benchmark_id": "traceh-adaptive-decomposition-" + scenario_id,
            "task_type": "product_task",
            "task_settings": _settings(repository, scenario_id),
            "dataset": {
                "file": "dataset.json",
                "sha256": digest_bytes((root / "dataset.json").read_bytes()),
            },
            "assessment": {
                "scorer_id": "product-durable-v1",
                "version": 1,
                "requires_review": False,
                "rubric": None,
            },
        }
        write(root / "benchmark.json", manifest)
        write(root / "reference-output.json", reference)
        contract.setdefault("materials", {})[scenario_id] = {
            "manifest_digest": digest_bytes((root / "benchmark.json").read_bytes()),
            "dataset_digest": manifest["dataset"]["sha256"],
            "initial_tree_digest": case["sha256"],
            "verification_digest": digest_bytes(
                verification["commands"][0]["argv"][3].encode("utf-8")
            ),
            "reference_digest": digest_bytes((root / "reference-output.json").read_bytes()),
        }
    contract["driver_digest"] = digest_bytes(Path(__file__).read_bytes())
    write(output / "contract.json", contract)


def preflight(output: Path) -> None:
    contract = read_input(output, "contract.json")
    contract.verify()
    sandbox = load_sandbox_file(output / "sandbox.json")
    image = sandbox.policy.image
    docker_context = sandbox.policy.docker_context
    if not image.startswith("sha256:") or not docker_context or sandbox.policy.network != "none":
        raise ValueError("adaptive-decomposition-sandbox-contract-invalid")
    rows = []
    for scenario_id in contract.data["scenario_order"]:
        material = output / "materials" / scenario_id
        case = read_input(material, "dataset.json").data["cases"][0]
        verifier = case["verification"]["commands"][0]["argv"][3]
        outcomes = []
        for repaired in (False, True):
            with TemporaryDirectory(prefix="traceh-da6-material-") as temporary:
                work = Path(temporary).resolve()
                if work.parent != Path(gettempdir()).resolve():
                    raise ValueError("adaptive-decomposition-temporary-owner-mismatch")
                shutil.copytree(material / "initial", work, dirs_exist_ok=True)
                if repaired:
                    shutil.copy2(material / "reference-output.json", work / "answer.json")
                command = [
                    "docker",
                    "--context",
                    docker_context,
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
                    image,
                    "python",
                    "-B",
                    "-c",
                    verifier,
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", timeout=60
                )
                valid = result.returncode == (0 if repaired else 1)
                if not repaired:
                    valid = valid and "AssertionError" in result.stderr
                outcomes.append(
                    {
                        "reference_output": repaired,
                        "exit_code": result.returncode,
                        "contract_verified": valid,
                    }
                )
        rows.append({"scenario": scenario_id, "outcomes": outcomes})
        print(
            json.dumps(
                {"scenario": scenario_id, "verified": all(x["contract_verified"] for x in outcomes)}
            ),
            flush=True,
        )
    result = {"format": 1, "image": image, "network": "none", "scenarios": rows}
    write(output / "preflight.json", result)
    if not all(x["contract_verified"] for row in rows for x in row["outcomes"]):
        raise ValueError("adaptive-decomposition-preflight-failed")


def _analyse(run: Path, provider: CappedProvider, scenario: dict) -> dict:
    report = read_input(run, "report.json").data
    attempt = report["task_report"]["attempts"][0]
    events = events_at(run / attempt["directory"] / "ev/events.sqlite3")
    results = [event for event in events if event["type"] == "tool/result"]
    succeeded = Counter(
        event["data"]["tool_name"] for event in results if event["data"]["status"] == "succeeded"
    )
    failed = Counter(
        event["data"]["tool_name"] for event in results if event["data"]["status"] == "failed"
    )
    delegation_calls = [
        event
        for event in events
        if event["type"] == "tool/call" and event["data"]["tool_name"] == "delegate_investigation"
    ]
    signatures = Counter(fingerprint(event["data"]["arguments"]) for event in delegation_calls)
    repeated = sum(count - 1 for count in signatures.values() if count > 1)
    expected = scenario["kind"] == "separable"
    execution = attempt["evidence"]["execution"]
    collaboration = attempt["evidence"]["collaboration"]
    facts = {
        "task_success": attempt["success"],
        "error": attempt["error_code"],
        "real_calls": provider.calls,
        "provider_calls_by_role": dict(provider.roles),
        "delegate_succeeded": succeeded["delegate_investigation"],
        "delegate_failed": failed["delegate_investigation"],
        "collect_succeeded": succeeded["collect_investigation"],
        "repeated_identical_delegations": repeated,
        "reports_dispatched_to_parent": collaboration["reports_dispatched_to_parent"],
        "budget_converged": attempt["evidence"]["budget"]["converged"],
        "live_workspaces": attempt["evidence"]["workspaces"]["live"],
        "total_tokens": execution["tokens"],
        "provider_failure_categories": execution["provider_failure_categories"],
        "termination": (
            "local-call-cap"
            if provider.limit_reached
            else "provider-failure"
            if execution["provider_failure_categories"]
            else "completed"
        ),
    }
    facts["behavior_pass"] = (
        facts["task_success"]
        and facts["delegate_failed"] == 0
        and facts["repeated_identical_delegations"] == 0
        and (
            facts["delegate_succeeded"] > 0
            and facts["collect_succeeded"] > 0
            and facts["provider_calls_by_role"].get("child", 0) > 0
            and facts["reports_dispatched_to_parent"] > 0
            if expected
            else facts["delegate_succeeded"] == 0
            and facts["provider_calls_by_role"].get("child", 0) == 0
        )
    )
    return facts


async def run(profile: Path, output: Path) -> None:
    contract = read_input(output, "contract.json")
    contract.verify()
    if not (output / "preflight.json").is_file():
        raise ValueError("adaptive-decomposition-preflight-required")
    driver = await asyncio.to_thread(Path(__file__).read_bytes)
    if (
        source_digest(source_files()[1]) != contract.data["source_digest"]
        or digest_bytes(driver) != contract.data["driver_digest"]
        or read_input(output, "sandbox.json").sha256 != contract.data["sandbox_digest"]
    ):
        raise ValueError("adaptive-decomposition-source-drift")
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    args, inner, model = connection(profile)
    if (inner.name, model) != ("openai-compatible", "qwen-plus"):
        raise ValueError("adaptive-decomposition-model-contract-mismatch")
    inner.timeout_seconds = 60
    sandbox = load_sandbox_file(output / "sandbox.json")
    if sandbox.plugin_grants or sandbox.policy.network != "none":
        raise ValueError("adaptive-decomposition-sandbox-scope-invalid")
    outcomes = []
    stopped = None
    for scenario_id in contract.data["scenario_order"]:
        scenario = contract.data["scenarios"][scenario_id]
        material = output / "materials" / scenario_id
        frozen = contract.data["materials"][scenario_id]
        if (
            read_input(material, "benchmark.json").sha256 != frozen["manifest_digest"]
            or read_input(material, "dataset.json").sha256 != frozen["dataset_digest"]
            or read_input(material, "reference-output.json").sha256 != frozen["reference_digest"]
        ):
            raise ValueError("adaptive-decomposition-material-drift")
        provider = CappedProvider(inner, scenario["max_real_calls"])
        target = output / "runs" / scenario_id
        runner = EvaluationRunner(
            material,
            target,
            provider=provider,
            model_id=model,
            retry_policy=NO_MODEL_RETRY,
            sandbox=sandbox.policy,
            options=RunOptions(repetitions=1, max_trials=1, timeout_seconds=600),
            provider_binding={
                "connection_digest": fingerprint(args.base_url),
                "network_mode": "direct",
                "timeout_seconds": 60,
                "call_limit": scenario["max_real_calls"],
            },
        )
        print(json.dumps({"action": "run", "scenario": scenario_id}), flush=True)
        await runner.run()
        facts = _analyse(target, provider, scenario)
        outcomes.append({"scenario": scenario_id, **facts})
        print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)
        if facts["termination"] == "provider-failure":
            stopped = "provider-failure"
            break
    summary = {
        "format": 1,
        "contract_digest": contract.sha256,
        "provider": inner.name,
        "model": model,
        "outcomes": outcomes,
        "stopped": stopped,
        "total_real_calls": sum(item["real_calls"] for item in outcomes),
        "all_behavior_pass": len(outcomes) == len(SCENARIOS)
        and all(item["behavior_pass"] for item in outcomes),
        "interpretation": (
            "Observed current Adaptive behavior only. No baseline, semantic judge, "
            "significance, or automatic adoption."
        ),
    }
    write(output / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.prepare:
        if options.sandbox is None:
            parser.error("--sandbox is required with --prepare")
        prepare(options.repository.resolve(), options.sandbox.resolve(), options.output.resolve())
    elif options.preflight:
        preflight(options.output.resolve())
    else:
        if options.profile is None:
            parser.error("--profile is required with --run")
        asyncio.run(run(options.profile.resolve(), options.output.resolve()))
