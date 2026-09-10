"""Paired workers also enter the original Product/Git/verification/promotion owner."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sandbox_fixtures import real_sandbox_policy
from test_evaluation_comparison import pair_plan
from test_product_benchmark_e2e import build_benchmark

from traceh.api.json_types import to_json_value
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.inputs import digest_bytes
from traceh.promotion.events import PROMOTION_LEDGER_STREAM


async def test_product_pair_uses_real_git_and_sandbox_without_a_second_success_rule():
    policy = real_sandbox_policy()
    with TemporaryDirectory(prefix="ue3p-") as directory:
        root = Path(directory)
        benchmark = build_benchmark(root / "b", arms=(("single", 1),))
        plan_file = pair_plan(root, execution={"timeout_seconds": 180})
        plan = json.loads(plan_file.read_text())
        plan["benchmark_digest"] = digest_bytes((benchmark / "benchmark.json").read_bytes())
        plan["trials"] = {"repetitions": 1}
        plan["execution"]["sandbox_config"] = "sandbox.json"
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
        assert result["quality_status"] == "no_change" and result["changes"]["unchanged"] == 1
        for index in (1, 2):
            report = json.loads((output / f"arms/{index:02d}/run/report.json").read_text())
            trial = report["trials"][0]
            assert trial["assessment"]["status"] == "passed"
            assert trial["convergence"] == "converged"
            assert any(r["stream_id"].startswith("product-task:") for r in trial["evidence"])
            assert any(r["stream_id"] == PROMOTION_LEDGER_STREAM for r in trial["evidence"])
