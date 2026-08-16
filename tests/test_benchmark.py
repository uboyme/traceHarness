from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from traceh.evaluation.runner import BenchmarkRunner


@pytest.mark.asyncio
async def test_benchmark_runner_generates_reports(tmp_path) -> None:
    source = Path(__file__).resolve().parents[1] / "benchmarks" / "basic"
    benchmark = tmp_path / "basic"
    shutil.copytree(source, benchmark)
    output = tmp_path / "output"
    report = await BenchmarkRunner(benchmark, output).run()
    assert report.success_rate == 1.0
    assert (output / "report.json").exists()
    raw = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert raw["cases"][0]["success"] is True
