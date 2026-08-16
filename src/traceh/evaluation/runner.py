"""Run deterministic or real-provider benchmark cases and verify the world."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.verification import CommandVerifier


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    task: str
    verify_command: str
    initial_dir: Path
    script_file: Path


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    success: bool
    session_id: str | None
    reason: str
    steps: int
    duration_seconds: float
    verification_summary: str
    invariant_violations: int
    tool_errors: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    benchmark: str
    cases: tuple[CaseResult, ...]
    success_rate: float
    total_duration_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark": self.benchmark,
            "cases": [asdict(case) for case in self.cases],
            "success_rate": self.success_rate,
            "total_duration_seconds": self.total_duration_seconds,
        }


class BenchmarkRunner:
    def __init__(self, benchmark_dir: Path, output_dir: Path) -> None:
        self.benchmark_dir = benchmark_dir.resolve()
        self.output_dir = output_dir.resolve()

    def discover(self) -> tuple[BenchmarkCase, ...]:
        cases = []
        for manifest in sorted(self.benchmark_dir.glob("*/case.json")):
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            case_dir = manifest.parent
            cases.append(
                BenchmarkCase(
                    name=str(raw.get("name") or case_dir.name),
                    task=str(raw["task"]),
                    verify_command=str(raw["verify_command"]),
                    initial_dir=case_dir / str(raw.get("initial_dir", "initial")),
                    script_file=case_dir / str(raw.get("script", "script.json")),
                )
            )
        return tuple(cases)

    async def run(self) -> BenchmarkReport:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        results = []
        for case in self.discover():
            results.append(await self._run_case(case))
        total = time.perf_counter() - started
        success_count = sum(item.success for item in results)
        report = BenchmarkReport(
            self.benchmark_dir.name,
            tuple(results),
            success_count / len(results) if results else 0.0,
            total,
        )
        (self.output_dir / "report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_markdown(report)
        return report

    async def _run_case(self, case: BenchmarkCase) -> CaseResult:
        run_dir = self.output_dir / case.name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        workspace = run_dir / "workspace"
        shutil.copytree(case.initial_dir, workspace)
        provider = ScriptedLlmProvider.from_file(case.script_file)
        verifier = CommandVerifier(case.verify_command)
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=run_dir / ".traceh",
                provider="scripted",
                model="benchmark-script",
                max_steps=20,
            ),
            provider=provider,
            verifier=verifier,
        )
        started = time.perf_counter()
        session_id: str | None = None
        try:
            session_id = await runtime.create_session(workspace, metadata={"benchmark": case.name})
            result = await runtime.run_existing(session_id, case.task)
            verification = await verifier.verify(workspace)
            violations = await runtime.check_invariants(session_id)
            events = await runtime.sessions.read_session(session_id)
            tool_errors = sum(
                1
                for event in events
                if event.type == "tool/result" and event.data.get("status") != "succeeded"
            )
            success = verification.passed and not violations
            return CaseResult(
                case.name,
                success,
                session_id,
                result.reason,
                result.steps,
                time.perf_counter() - started,
                verification.summary,
                len(violations),
                tool_errors,
            )
        except BaseException as error:
            return CaseResult(
                case.name,
                False,
                session_id,
                "runtime_error",
                0,
                time.perf_counter() - started,
                "verification not completed",
                0,
                0,
                f"{type(error).__name__}: {error}",
            )
        finally:
            await runtime.dispose()

    def _write_markdown(self, report: BenchmarkReport) -> None:
        lines = [
            f"# Benchmark: {report.benchmark}",
            "",
            f"Success rate: **{report.success_rate:.1%}**",
            f"Total duration: **{report.total_duration_seconds:.2f}s**",
            "",
            "| Case | Success | Steps | Tool errors | Invariants | Duration |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for case in report.cases:
            lines.append(
                f"| {case.name} | {'yes' if case.success else 'no'} | {case.steps} | "
                f"{case.tool_errors} | {case.invariant_violations} | "
                f"{case.duration_seconds:.2f}s |"
            )
        (self.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark_sync(benchmark_dir: Path, output_dir: Path) -> BenchmarkReport:
    return asyncio.run(BenchmarkRunner(benchmark_dir, output_dir).run())
