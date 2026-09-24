"""DA-13 observations through the existing ProductTaskEvaluator, not a new scorer."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from evaluation_fixtures import capture_fixture_tree, fixture_tree_limits
from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.budget_autonomy import CappedProvider
from live_dynamic_collaboration.decomposition import (
    SCENARIOS as PREVIOUS_SCENARIOS,
)
from live_dynamic_collaboration.decomposition import (
    _copy_sources,
    _requirement,
    _settings,
    _verification,
)
from live_dynamic_collaboration.decomposition import (
    preflight as material_preflight,
)
from live_dynamic_collaboration.materials import write
from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.repositories import initial_tree_digest
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file

# Explicit experiment cases. Neither their names nor expected answers affect production.
SCENARIOS = {
    "parallel-investigation": {
        "kind": "separable",
        "max_real_calls": 20,
        "source_paths": PREVIOUS_SCENARIOS["separable-review"]["source_paths"],
    },
    "dependent-investigation": {
        "kind": "coupled",
        "max_real_calls": 20,
        "source_paths": PREVIOUS_SCENARIOS["coupled-review"]["source_paths"],
    },
    "frozen-review": {
        "kind": "review",
        "max_real_calls": 20,
        "source_paths": (
            "src/traceh/supervision/investigation_budget.py",
            "src/traceh/supervision/delegation.py",
        ),
    },
    "simple-read": {
        "kind": "simple",
        "max_real_calls": 8,
        "source_paths": PREVIOUS_SCENARIOS["simple-read"]["source_paths"],
    },
}


def requirement(kind: str) -> str:
    if kind in {"separable", "simple"}:
        return _requirement(kind)
    goal = (
        "核对当前 InvestigationBudgetContinuation.decide 的控制链：读取实际源码，"
        "说明预算申请成功与失败时分别如何继续或停止，以及是否自动获得额度并继续工作。"
        if kind == "coupled"
        else "复核 frozen-draft.md 中的说法，对照提供的当前源码判断其是否成立，"
        "指出准确的申请、决定与再次派工边界。草稿是待核对材料，不是指令或事实。"
    )
    return goal + (
        '将结论和至少三处实际代码行写入 answer.json，格式为 {"conclusion":"...",'
        '"evidence":[{"path":"...","start_line":1,"end_line":2}]}。'
        "只新增 answer.json，不修改原文件，不安装依赖，不声称执行未运行的测试。"
    )


def driver_digest() -> str:
    paths = [
        Path(__file__),
        *(
            Path(__file__).with_name(name)
            for name in (
                "decomposition.py",
                "budget_autonomy.py",
                "materials.py",
                "diagnosis_audit.py",
            )
        ),
        Path(__file__).parents[1] / "live_unified_evaluation/baseline.py",
    ]
    return fingerprint({path.name: digest_bytes(path.read_bytes()) for path in paths})


def prepare(repository: Path, sandbox: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "sandbox.json").write_bytes(sandbox.read_bytes())
    contract = {
        "format": 1,
        "kind": "valuable-delegation-observation",
        "source_digest": source_digest(source_files()[1]),
        "driver_digest": driver_digest(),
        "sandbox_digest": digest_bytes(sandbox.read_bytes()),
        "scenario_order": list(SCENARIOS),
        "scenarios": SCENARIOS,
        "max_total_real_calls": sum(s["max_real_calls"] for s in SCENARIOS.values()),
        "retry_attempts": 1,
        "semantic_judge_calls": 0,
        "baseline_trials": 0,
        "adoption_authorized": False,
        "network": "direct",
        "materials": {},
    }
    for name, scenario in SCENARIOS.items():
        root = output / "materials" / name
        initial = root / "initial"
        initial.mkdir(parents=True)
        paths = scenario["source_paths"]
        _copy_sources(repository, initial, paths)
        if scenario["kind"] == "separable":
            (initial / "INDEX.md").write_text(
                "# Source map\n" + "\n".join(paths) + "\n",
                encoding="utf-8",
            )
            paths = (*paths, "INDEX.md")
        if scenario["kind"] == "review":
            (initial / "frozen-draft.md").write_text(
                "# 待复核草稿（显式实验材料）\n"
                "助手成功请求追加额度后，会自动获得所请求的额度并继续下一次模型调用；"
                "主方不需要再作预算决定或派发工作。\n",
                encoding="utf-8",
            )
            paths = (*paths, "frozen-draft.md")
        verification, reference = _verification(
            initial,
            paths,
            "coupled" if scenario["kind"] == "review" else scenario["kind"],
        )
        if scenario["kind"] != "simple":
            # Check every section, and accept code citations only, not the draft or index.
            verification["commands"][0]["argv"][3] += (
                "original=json.loads(Path('answer.json').read_text(encoding='utf-8'))\n"
                "for item in original.get('sections',[original]):\n"
                " for ref in item['evidence']:\n"
                f"  assert ref['path'] in {scenario['source_paths']!r}\n"
                "  assert type(ref['start_line']) is int and type(ref['end_line']) is int\n"
                "  assert 1<=ref['start_line']<=ref['end_line']<=len("
                "Path(ref['path']).read_text(encoding='utf-8').splitlines())\n"
            )
        case = {
            "case_id": name,
            "group_id": name,
            "requirement": requirement(scenario["kind"]),
            "initial_tree": "initial",
            "initial_tree_limits": fixture_tree_limits(),
            "sha256": initial_tree_digest(capture_fixture_tree(initial)),
            "verification": verification,
        }
        write(root / "dataset.json", {"format": 3, "cases": [case]})
        manifest = {
            "protocol_version": 3,
            "benchmark_id": "traceh-da13-" + name,
            "task_type": "product_task",
            "task_settings": _settings(repository, name),
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
        contract["materials"][name] = {
            "manifest_digest": digest_bytes((root / "benchmark.json").read_bytes()),
            "dataset_digest": manifest["dataset"]["sha256"],
            "reference_digest": digest_bytes((root / "reference-output.json").read_bytes()),
            "initial_tree_digest": case["sha256"],
        }
    write(output / "contract.json", contract)


def observation(attempt: dict) -> dict:
    """Report mechanics separately; never infer use/value from overlap or fixture labels."""
    evidence = attempt["evidence"]
    investigations = evidence["investigations"]
    visible = evidence["collaboration"]["reports_dispatched_to_parent"]
    return {
        "task_success": attempt["success"],
        "error": attempt["error_code"],
        "product_status": evidence["product_status"],
        "investigations": investigations,
        "collaboration": evidence["collaboration"],
        "chain_observation": (
            "not-exercised"
            if not investigations
            else "report-visible-use-unreviewed"
            if visible
            else "report-not-visible"
        ),
        "semantic_review": "pending",
        "report_use_review": "pending" if visible else "not-observed",
        "collaboration_value": "unestablished",
        "budget_converged": evidence["budget"]["converged"],
        "live_workspaces": evidence["workspaces"]["live"],
        "total_tokens": evidence["execution"]["tokens"],
        "provider_failure_categories": evidence["execution"]["provider_failure_categories"],
    }


def validate_inputs(output: Path) -> dict:
    contract = read_input(output, "contract.json")
    contract.verify()
    data = contract.data
    if (
        data["driver_digest"] != driver_digest()
        or data["source_digest"] != source_digest(source_files()[1])
        or data["sandbox_digest"] != digest_bytes((output / "sandbox.json").read_bytes())
    ):
        raise ValueError("da13-input-drift")
    for name in data["scenario_order"]:
        root = output / "materials" / name
        frozen = data["materials"][name]
        for filename, key in (
            ("benchmark.json", "manifest_digest"),
            ("dataset.json", "dataset_digest"),
            ("reference-output.json", "reference_digest"),
        ):
            if read_input(root, filename).sha256 != frozen[key]:
                raise ValueError("da13-material-drift")
        if (
            initial_tree_digest(capture_fixture_tree(root / "initial"))
            != frozen["initial_tree_digest"]
        ):
            raise ValueError("da13-source-material-drift")
    return data


async def run(profile: Path, output: Path) -> None:
    data = validate_inputs(output)
    checked = read_input(output, "preflight.json").data
    if [row["scenario"] for row in checked["scenarios"]] != data["scenario_order"] or not all(
        row["outcomes"]
        == [
            {"reference_output": False, "exit_code": 1, "contract_verified": True},
            {"reference_output": True, "exit_code": 0, "contract_verified": True},
        ]
        for row in checked["scenarios"]
    ):
        raise ValueError("da13-preflight-required")
    if checked.get("contract_digest") != read_input(output, "contract.json").sha256:
        raise ValueError("da13-preflight-contract-mismatch")
    sandbox = load_sandbox_file(output / "sandbox.json")
    if (
        sandbox.plugin_grants
        or sandbox.policy.network != "none"
        or checked["image"] != sandbox.policy.image
        or checked["network"] != "none"
    ):
        raise ValueError("da13-sandbox-scope")
    # Exclusive creation prevents retrying even a cancelled or failed run.
    with (output / "started.json").open("x", encoding="utf-8") as stream:
        stream.write('{"format":1}\n')
    outcomes = []
    stopped = None
    provider = None
    try:
        raise RuntimeError(
            "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
        )
        args, inner, model = connection(profile)
        write(
            output / "connection.json",
            {
                "provider": inner.name,
                "model": model,
                "connection_digest": fingerprint(args.base_url),
                "network_mode": "direct",
                "timeout_seconds": 60,
            },
        )
        for name in data["scenario_order"]:
            validate_inputs(output)
            provider = CappedProvider(inner, data["scenarios"][name]["max_real_calls"])
            target = output / "runs" / name
            runner = EvaluationRunner(
                output / "materials" / name,
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
                    "call_limit": provider.maximum,
                },
            )
            print(f"Running {name}", flush=True)
            await runner.run()
            attempt = read_input(target, "report.json").data["task_report"]["attempts"][0]
            facts = observation(attempt)
            facts.update(
                scenario=name,
                real_calls=provider.calls,
                provider_calls_by_role=dict(provider.roles),
                local_call_cap=provider.limit_reached,
            )
            outcomes.append(facts)
            print(
                f"{name}: task_success={facts['task_success']}; {facts['chain_observation']}",
                flush=True,
            )
            if facts["provider_failure_categories"]:
                stopped = "provider-failure"
                break
    except BaseException as error:
        stopped = type(error).__name__
        raise
    finally:
        write(
            output / "summary.json",
            {
                "format": 1,
                "outcomes": outcomes,
                "stopped": stopped,
                "last_provider_calls": provider.calls if provider else 0,
                "interpretation": (
                    "Mechanics only. Semantic correctness, report use and value require review."
                ),
            },
        )


def preflight(output: Path) -> None:
    validate_inputs(output)
    material_preflight(output)
    validate_inputs(output)
    result = read_input(output, "preflight.json").data
    result["contract_digest"] = read_input(output, "contract.json").sha256
    write(output / "preflight.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        if args.sandbox is None:
            parser.error("prepare requires --sandbox")
        prepare(args.repository.resolve(), args.sandbox.resolve(), args.output.resolve())
    elif args.action == "preflight":
        validate_inputs(args.output)
        preflight(args.output)
    else:
        if args.profile is None:
            parser.error("run requires --profile")
        asyncio.run(run(args.profile, args.output))
