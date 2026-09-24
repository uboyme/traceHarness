"""Three bounded real-model observations of investigator budget autonomy.

This is an opt-in mechanism diagnostic. It builds explicit test materials,
freezes them before connecting to a provider, and reuses EvaluationRunner.
It is not a baseline comparison, a semantic judge, or an autonomy score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from evaluation_fixtures import capture_fixture_tree, fixture_tree_limits
from live_unified_evaluation.baseline import connection

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
    "complex-headroom": {
        "initial_tokens": 12_000,
        "ceiling_tokens": 30_000,
        "max_real_calls": 16,
        "material": "complex",
    },
    "sufficient-simple": {
        "initial_tokens": 12_000,
        "ceiling_tokens": 30_000,
        "max_real_calls": 5,
        "material": "simple",
    },
    "constrained-headroom": {
        "initial_tokens": 12_000,
        "ceiling_tokens": 15_000,
        "max_real_calls": 16,
        "material": "complex",
    },
}
SOURCE_PATHS = (
    "src/traceh/api/budgets.py",
    "src/traceh/budgets/events.py",
    "src/traceh/budgets/projection.py",
    "src/traceh/budgets/service.py",
    "src/traceh/budgets/supervision.py",
    "src/traceh/supervision/delegation.py",
    "src/traceh/supervision/investigation_budget.py",
    "src/traceh/product/resources.py",
    "src/traceh/product/runtime.py",
)
TOPICS = ("allocation", "request", "continuation")


def _complex_requirement(constrained: bool) -> str:
    suffix = (
        "本次调查方的追加空间很小；额度不足时应保留已找到的证据并明确未知，不要猜测。"
        if constrained
        else "证据较分散；先定位相关实现，再决定是否需要继续调查。"
    )
    return (
        "评审这个快照中的只读助手额度协商，交付 answer.json。先读取 INDEX.md，分别说明："
        "(1) 初始 Token、累计上限和父方保留量如何约束分配；"
        "(2) 助手的申请如何成为可核对证据，主方如何批准或拒绝；"
        "(3) 为什么批准后还要显式续派，以及停止、取消和其他预算是否被重置。"
        "三个主题分别使用 allocation、request、continuation，每项至少引用两处实际代码行。"
        '输出格式为 {"sections":[{"topic":"allocation","conclusion":"...",'
        '"evidence":[{"path":"...","start_line":1,"end_line":2}]}]}。'
        "每个主题恰好一次，只新增 answer.json。可以按需使用只读助手分担独立核对，"
        "但主方负责检查交接证据和最终结论。不要安装依赖，不要声称运行了未运行的测试。" + suffix
    )


def _simple_requirement() -> str:
    return (
        "读取 src/traceh/version.py，把代码中的当前版本写入 answer.json，格式为"
        ' {"version":"实际版本"}。只新增这一个文件并检查内容。'
    )


def _copy_file(repository: Path, material: Path, relative: str) -> bytes:
    data = (repository / relative).read_bytes()
    target = material / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return data


def _verification(material: Path, source_paths: tuple[str, ...], *, simple: bool) -> dict:
    hashes = {name: digest_bytes((material / name).read_bytes()) for name in source_paths}
    if simple:
        source = (material / source_paths[0]).read_text(encoding="utf-8")
        version = source.split('__version__ = "', 1)[1].split('"', 1)[0]
        body = (
            "import hashlib,json\nfrom pathlib import Path\n"
            f"expected={hashes!r}\n"
            "assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h "
            "for n,h in expected.items())\n"
            "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
            "and '.git' not in p.parts}\n"
            "assert files-set(expected)=={'answer.json'}, files\n"
            f"assert json.loads(Path('answer.json').read_text(encoding='utf-8'))=="
            f"{{'version':{version!r}}}\n"
        )
    else:
        body = (
            "import hashlib,json\nfrom pathlib import Path\n"
            f"expected={hashes!r}\n"
            "assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h "
            "for n,h in expected.items())\n"
            "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
            "and '.git' not in p.parts}\n"
            "assert files-set(expected)=={'answer.json'}, files\n"
            "answer=json.loads(Path('answer.json').read_text(encoding='utf-8'))\n"
            "assert set(answer)=={'sections'} and len(answer['sections'])==3\n"
            f"assert {{s['topic'] for s in answer['sections']}}=={set(TOPICS)!r}\n"
            "for section in answer['sections']:\n"
            " assert isinstance(section['conclusion'],str) and len(section['conclusion'])>=30\n"
            " assert isinstance(section['evidence'],list) and len(section['evidence'])>=2\n"
            " for ref in section['evidence']:\n"
            "  assert set(ref)=={'path','start_line','end_line'} and ref['path'] in expected\n"
            "  lines=Path(ref['path']).read_text(encoding='utf-8').splitlines()\n"
            "  assert type(ref['start_line']) is int and type(ref['end_line']) is int\n"
            "  assert 1<=ref['start_line']<=ref['end_line']<=len(lines)\n"
        )
    return {
        "plan_id": "budget-autonomy-output-contract",
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
            "policy_id": "budget-autonomy-no-dependencies",
            "passthrough": [],
            "overrides": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
        },
        "max_output_bytes": 1_048_576,
    }


def _settings(repository: Path, scenario_id: str, scenario: dict) -> dict:
    template = json.loads(
        (repository / "benchmarks/dynamic_collaboration_v1/development/benchmark.json").read_text(
            encoding="utf-8"
        )
    )["task_settings"]
    template.update(
        profile_id="budget-autonomy-" + scenario_id,
        default_mode="adaptive",
        modes=["adaptive"],
        retained_tokens=40_000,
        investigator_initial_tokens=scenario["initial_tokens"],
        max_report_chars=12_000,
    )
    template["task_budget"] = budgets(320_000, 70, 90, 900_000, 2, 2, 2)
    template["roles"]["coder"].update(
        max_output_tokens=6_000,
        max_turn_wall_milliseconds=180_000,
        budget=budgets(200_000, 36, 50, 720_000, 1, 1, 1),
    )
    template["roles"]["investigator"].update(
        max_output_tokens=3_072,
        max_turn_wall_milliseconds=120_000,
        budget=budgets(scenario["ceiling_tokens"], 12, 16, 240_000, 0, 0, 0),
    )
    return template


def prepare(repository: Path, sandbox: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "sandbox.json").write_bytes(sandbox.read_bytes())
    source_hash = source_digest(source_files()[1])
    frozen = {
        "format": 1,
        "kind": "real-main-and-child-budget-autonomy-observation",
        "source_digest": source_hash,
        "sandbox_digest": digest_bytes(sandbox.read_bytes()),
        "network": "direct",
        "retry_attempts": 1,
        "scenario_order": list(SCENARIOS),
        "scenarios": SCENARIOS,
        "max_total_real_calls": sum(item["max_real_calls"] for item in SCENARIOS.values()),
        "semantic_judge_calls": 0,
        "baseline_trials": 0,
        "adoption_authorized": False,
        "interpretation": (
            "Three observations. Hard evidence only; no statistical significance or autonomy score."
        ),
    }
    materials = output / "materials"
    for scenario_id, scenario in SCENARIOS.items():
        root = materials / scenario_id
        initial = root / "initial"
        initial.mkdir(parents=True)
        simple = scenario["material"] == "simple"
        paths = ("src/traceh/version.py",) if simple else SOURCE_PATHS
        for relative in paths:
            _copy_file(repository, initial, relative)
        if not simple:
            index = "# Source map\n\n" + "\n".join(f"- `{name}`" for name in paths) + "\n"
            (initial / "INDEX.md").write_text(index, encoding="utf-8")
            paths = (*paths, "INDEX.md")
        case = {
            "case_id": scenario_id,
            "group_id": scenario_id,
            "requirement": (
                _simple_requirement()
                if simple
                else _complex_requirement(scenario_id == "constrained-headroom")
            ),
            "initial_tree": "initial",
            "initial_tree_limits": fixture_tree_limits(),
            "sha256": initial_tree_digest(capture_fixture_tree(initial)),
            "verification": _verification(initial, paths, simple=simple),
        }
        write(root / "dataset.json", {"format": 3, "cases": [case]})
        manifest = {
            "protocol_version": 3,
            "benchmark_id": "traceh-budget-autonomy-" + scenario_id,
            "task_type": "product_task",
            "task_settings": _settings(repository, scenario_id, scenario),
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
        frozen.setdefault("materials", {})[scenario_id] = {
            "manifest_digest": digest_bytes((root / "benchmark.json").read_bytes()),
            "dataset_digest": manifest["dataset"]["sha256"],
            "initial_tree_digest": case["sha256"],
        }
    frozen["driver_digest"] = digest_bytes(Path(__file__).read_bytes())
    write(output / "contract.json", frozen)


class CappedProvider:
    def __init__(self, inner, maximum: int):
        self.inner = inner
        self.name = inner.name
        self.maximum = maximum
        self.calls = 0
        self.roles = Counter()
        self.limit_reached = False

    async def complete(self, request):
        names = {tool.name for tool in request.tools}
        role = (
            "main"
            if "decide_investigation_budget" in names
            else "child"
            if "request_investigation_budget" in names
            else "unknown"
        )
        if self.calls >= self.maximum:
            self.limit_reached = True
            raise RuntimeError("budget-autonomy-real-call-limit")
        self.calls += 1
        self.roles[role] += 1
        return await self.inner.complete(request)


def _analyse(run: Path, provider: CappedProvider, scenario: dict) -> dict:
    report = read_input(run, "report.json").data
    attempt = report["task_report"]["attempts"][0]
    database = run / attempt["directory"] / "ev/events.sqlite3"
    events = events_at(database)
    calls = [event for event in events if event["type"] == "tool/call"]
    results = [event for event in events if event["type"] == "tool/result"]
    succeeded = Counter(
        event["data"]["tool_name"] for event in results if event["data"]["status"] == "succeeded"
    )
    failed = Counter(
        event["data"]["tool_name"] for event in results if event["data"]["status"] == "failed"
    )
    decisions = [event["data"] for event in events if event["type"] == "budget/child-token-decided"]
    signatures = Counter(
        fingerprint({"name": event["data"]["tool_name"], "args": event["data"]["arguments"]})
        for event in calls
        if event["data"]["tool_name"] == "decide_investigation_budget"
    )
    repeated_decisions = sum(count - 1 for count in signatures.values() if count > 1)
    facts = {
        "task_success": attempt["success"],
        "error": attempt["error_code"],
        "real_calls": provider.calls,
        "provider_calls_by_role": dict(provider.roles),
        "delegate_succeeded": succeeded["delegate_investigation"],
        "request_succeeded": succeeded["request_investigation_budget"],
        "decision_succeeded": succeeded["decide_investigation_budget"],
        "decision_failed": failed["decide_investigation_budget"],
        "decision_tokens": [item["tokens"] for item in decisions],
        "followup_succeeded": succeeded["followup_investigation"],
        "repeated_identical_decisions": repeated_decisions,
        "budget_converged": attempt["evidence"]["budget"]["converged"],
        "live_workspaces": attempt["evidence"]["workspaces"]["live"],
        "total_tokens": attempt["evidence"]["execution"]["tokens"],
        "provider_failure_categories": attempt["evidence"]["execution"][
            "provider_failure_categories"
        ],
        "termination": (
            "local-call-cap"
            if provider.limit_reached
            else "provider-failure"
            if attempt["evidence"]["execution"]["provider_failure_categories"]
            else "completed"
        ),
    }
    if scenario["material"] == "simple":
        facts["behavior_pass"] = (
            facts["task_success"]
            and facts["request_succeeded"] == 0
            and facts["decision_succeeded"] == 0
        )
        facts["efficient_no_delegation"] = facts["delegate_succeeded"] == 0
    elif scenario["ceiling_tokens"] == 15_000:
        facts["behavior_pass"] = (
            facts["task_success"]
            and facts["request_succeeded"] > 0
            and bool(decisions)
            and all(item["tokens"] <= 3_000 for item in decisions)
            and facts["repeated_identical_decisions"] == 0
        )
    else:
        facts["behavior_pass"] = (
            facts["task_success"]
            and facts["delegate_succeeded"] > 0
            and facts["request_succeeded"] > 0
            and any(item["tokens"] > 0 for item in decisions)
            and facts["followup_succeeded"] > 0
            and facts["repeated_identical_decisions"] == 0
        )
    return facts


async def run(profile: Path, output: Path) -> None:
    contract = read_input(output, "contract.json")
    contract.verify()
    driver = await asyncio.to_thread(Path(__file__).read_bytes)
    if (
        source_digest(source_files()[1]) != contract.data["source_digest"]
        or digest_bytes(driver) != contract.data["driver_digest"]
        or read_input(output, "sandbox.json").sha256 != contract.data["sandbox_digest"]
    ):
        raise ValueError("budget-autonomy-source-drift")
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    args, inner, model = connection(profile)
    if (inner.name, model) != ("openai-compatible", "qwen-plus"):
        raise ValueError("budget-autonomy-model-contract-mismatch")
    inner.timeout_seconds = 60
    sandbox = load_sandbox_file(output / "sandbox.json")
    if sandbox.plugin_grants or sandbox.policy.network != "none":
        raise ValueError("budget-autonomy-sandbox-scope-invalid")
    outcomes = []
    stopped = None
    for scenario_id in contract.data["scenario_order"]:
        scenario = contract.data["scenarios"][scenario_id]
        material = output / "materials" / scenario_id
        frozen = contract.data["materials"][scenario_id]
        if (
            read_input(material, "benchmark.json").sha256 != frozen["manifest_digest"]
            or read_input(material, "dataset.json").sha256 != frozen["dataset_digest"]
        ):
            raise ValueError("budget-autonomy-material-drift")
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
            "Observed behavior only. No baseline, semantic judge, significance, "
            "or adoption decision."
        ),
    }
    write(output / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.prepare == options.run:
        parser.error("choose exactly one of --prepare or --run")
    if options.prepare:
        if options.sandbox is None:
            parser.error("--sandbox is required with --prepare")
        prepare(options.repository.resolve(), options.sandbox.resolve(), options.output.resolve())
    else:
        if options.profile is None:
            parser.error("--profile is required with --run")
        asyncio.run(run(options.profile.resolve(), options.output.resolve()))
