"""Paired workers also enter the original Product/Git/verification/promotion owner."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sandbox_fixtures import real_sandbox_policy
from test_evaluation_comparison import pair_plan
from test_product_benchmark_e2e import build_benchmark

from traceh.api.json_types import to_json_value
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.inputs import digest_bytes
from traceh.promotion.events import PROMOTION_LEDGER_STREAM


@pytest.mark.parametrize("strategy", [False, True])
async def test_product_pair_uses_real_git_and_sandbox_without_a_second_success_rule(strategy):
    policy = real_sandbox_policy()
    with TemporaryDirectory(prefix="ue3p-") as directory:
        root = Path(directory)
        benchmark = build_benchmark(
            root / "b", arms=(("single", 1), ("multi", 1)) if strategy else (("single", 1),)
        )
        plan_file = pair_plan(root, execution={"timeout_seconds": 180})
        plan = json.loads(plan_file.read_text())
        plan["benchmark_digest"] = digest_bytes((benchmark / "benchmark.json").read_bytes())
        plan["trials"] = {"repetitions": 1}
        plan["execution"]["sandbox_config"] = "sandbox.json"
        if strategy:
            plan["comparison"].update(
                kind="execution_strategy", requested_modes=["single", "multi"]
            )
            plan["execution"]["first_arm"] = "candidate"
        plan_file.write_text(json.dumps(plan))
        (root / "sandbox.json").write_text(
            json.dumps({"format": 2, "policy": to_json_value(policy), "plugin_grants": []})
        )
        (root / "script.json").write_text(
            json.dumps(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "write-file",
                                "name": "apply_patch",
                                "arguments": {
                                    "path": "added.txt",
                                    "old_text": "",
                                    "new_text": "added\n",
                                    "create": True,
                                },
                            }
                        ],
                        "usage": {"input_tokens": 20, "output_tokens": 10},
                    },
                    {"content": "implemented", "usage": {"input_tokens": 20, "output_tokens": 10}},
                ]
            )
        )
        output = root / "out"
        args = build_parser().parse_args(
            [
                "eval",
                str(benchmark),
                "--run-plan",
                str(plan_file),
                "--output",
                str(output),
                "--env-file",
                str(root / "empty.env"),
            ]
        )
        _configure_from_environment(args)
        code = await _eval(args)
        result = json.loads((output / "comparison/report.json").read_text())
        assert code == 0 and result["complete"], result
        # This fixed two-response script writes immediately. It is valid for
        # single, but violates Multi's scout/decision contract. The shared
        # evaluator must expose that real failure, not invent an A/A tie.
        assert result["quality_status"] == ("regressed" if strategy else "no_change")
        assert result["changes"]["loss" if strategy else "unchanged"] == 1
        for index in (1, 2):
            report = json.loads((output / f"arms/{index:02d}/run/report.json").read_text())
            trial = report["trials"][0]
            passed = not (strategy and index == 2)
            assert trial["assessment"]["status"] == ("passed" if passed else "failed")
            assert trial["convergence"] == "converged"
            assert any(r["stream_id"].startswith("product-task:") for r in trial["evidence"])
            assert (
                any(r["stream_id"] == PROMOTION_LEDGER_STREAM for r in trial["evidence"]) is passed
            )
            assert trial["identity"]["requested_mode"] == (
                "multi" if strategy and index == 2 else "single"
            )
            assert (
                len(report["task_report"]["attempts"][0]["evidence"]["execution"]["sessions"]) == 1
            )
        execution = json.loads((output / "execution.json").read_text())
        assert [o["variant_id"] for o in execution["outcomes"]] == (
            ["experiment", "reference"] if strategy else ["reference", "experiment"]
        )
