"""UE-1 owns scheduling while Product keeps its production success rules."""

import ast
from pathlib import Path

import traceh.evaluation as evaluation
from traceh.evaluation.contracts import Evaluator
from traceh.evaluation.evaluators.product import ProductTaskEvaluator


def test_common_result_and_contracts_do_not_import_product_owners():
    root = Path(evaluation.__file__).parent
    for name in ("contracts.py", "inputs.py", "manifest.py", "plan.py", "report.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        assert not any(
            module.startswith(
                (
                    "traceh.product",
                    "traceh.workflow",
                    "traceh.promotion",
                    "traceh.evaluation.evaluators",
                )
            )
            for module in imported
        ), name


def test_product_adapter_implements_the_shared_contract_and_old_entry_is_removed():
    for method in (
        "trials",
        "verify_inputs",
        "frozen_settings",
        "frozen_materials",
        "execute",
        "summarize",
    ):
        assert callable(getattr(Evaluator, method))
        assert callable(getattr(ProductTaskEvaluator, method))
    assert not hasattr(evaluation, "ProductBenchmarkRunner")
    root = Path(evaluation.__file__).parent
    assert not (root / "metrics.py").exists()
    assert "EvaluationRunner" in evaluation.__all__
