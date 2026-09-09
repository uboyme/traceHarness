"""Explicit E1 real Provider journey; fresh directories, no pytest auto-dispatch."""

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from uuid import uuid4

from traceh.api.history import HistoryReadPolicy
from traceh.cli.credentials import load_key
from traceh.cli.main import _configure_from_environment, _provider_and_model, build_parser
from traceh.cli.tui_entry import initial_settings
from traceh.llm.retry import ModelRetryPolicy
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.compaction import CompactionPolicy
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision


class IsolatedPolicy:
    name = "isolated-in-turn-acceptance"

    async def check(self, call, tool, context):
        allowed = call.name in {
            "request_history_page",
            "list_tool_outputs",
            "search_tool_output",
            "read_tool_output",
            "list_files",
        }
        if call.name == "read_file":
            allowed = call.arguments.get("path") in {"verify.py", "./verify.py", ".\\verify.py"}
        if call.name == "shell":
            argv = shlex.split(call.arguments.get("command", ""))
            allowed = (
                len(argv) == 2
                and Path(argv[0]) == Path(sys.executable)
                and argv[1] == "verify.py"
                and not (context.workspace / "executions.txt").exists()
            )
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "Inspect the fixture, run its supplied command once, or read Session evidence.",
            self.name,
        )


async def run(arguments):
    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    args = build_parser().parse_args(
        [
            "chat",
            "--tui",
            "--tui-profile",
            str(arguments.profile),
            "--auto-compact",
            "off",
            "--auto-compact-method",
            "extractive",
        ]
    )
    args.tui_explicit = {"auto_compact", "auto_compact_method"}
    args, error = initial_settings(args)
    if error:
        raise RuntimeError("live-profile-invalid")
    environment = dict(os.environ)
    _configure_from_environment(args, environment=environment)
    args.tui_api_key = environment.get(args.api_key_env) or load_key(args)
    provider, model = _provider_and_model(args)
    provider.timeout_seconds = 45
    report = {"model": model, "scripted_provider": False, "cases": [], "completed": False}

    def save():
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for exit_code in (0, 7):
        folder = root / f"exit-{exit_code}"
        work = folder / "workspace"
        work.mkdir(parents=True)
        nonce = uuid4().hex[:12]
        (work / "verify.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "with Path('executions.txt').open('a') as f: f.write('x')\n"
            "for i in range(1000): print(f'record {i}: validation observation complete')\n"
            f"print('END_RECORD={nonce}; exit_code={exit_code}')\nsys.exit({exit_code})\n",
            encoding="utf-8",
        )
        case = {"exit_code": exit_code, "nonce": nonce, "turns": []}
        report["cases"].append(case)
        store = SqliteEventStore(folder / "data" / "events")

        def make(maintenance=False, *, folder=folder, store=store):
            return build_default_runtime(
                RuntimeConfig(
                    data_dir=folder / "data",
                    provider=provider.name,
                    model=model,
                    max_steps=16,
                    max_output_tokens=3072,
                    max_tool_output_chars=50_000,
                    context_input=ContextInputPolicy(
                        history_tier="directory",
                        total_bytes=30000,
                        history_bytes=24000,
                        item_bytes=16000,
                        max_query_bytes=16000,
                        max_blocks=8,
                        max_exclusions=12,
                        history=HistoryReadPolicy(
                            max_blocks=8,
                            max_depth=16,
                            page_bytes=16000,
                            page_messages=24,
                            max_source_events=5000,
                            max_source_bytes=8_000_000,
                            max_requests=16,
                        ),
                    ),
                    token_budget=TokenBudgetPolicy("cl100k_base", 21_120, 3072, 2048, 50),
                    compaction=CompactionPolicy(True, 10_000_000, 6000, 1) if maintenance else None,
                    semantic_summary=maintenance,
                    model_retry_policy=ModelRetryPolicy(4, 240, 1, 5, 5, 0),
                ),
                provider=provider,
                event_store=store,
                policies=(IsolatedPolicy(),),
            )

        async def turn(runtime, sid, label, question, expected=(), *, case=case):
            start = len(await runtime.sessions.read_session(sid))
            row = {"label": label, "question": question}
            case["turns"].append(row)
            save()
            try:
                result = await runtime.run_existing(sid, question)
                row.update(
                    answer=result.final_text,
                    steps=result.steps,
                    expected_found=all(value in result.final_text for value in expected),
                )
            finally:
                events = await runtime.sessions.read_session(sid)
                row["attempts"] = [e.data for e in events[start:] if e.type == "model/attempt-end"]
                row["measurements"] = [
                    e.data["measurement"]
                    for e in events[start:]
                    if e.type == "request/token-measurement"
                ]
                row["events"] = [
                    {"type": e.type, "seq": e.seq, "step_id": e.data.get("step_id")}
                    for e in events[start:]
                ]
                save()
            assert not await runtime.check_invariants(sid)
            assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
            assert row["expected_found"], label
            print(
                label,
                json.dumps({"answer": row["answer"], "steps": row["steps"]}, ensure_ascii=False),
                flush=True,
            )
            return events[start:]

        runtime = make()
        try:
            sid = await runtime.create_session(work)
            case["session_id"] = sid
            old = "\n".join(
                f"过期待核对字段 {i:03d}: candidate_{uuid4().hex[:12]}" for i in range(100)
            )
            await turn(
                runtime,
                sid,
                "old-requirements",
                "我们做离线档案校验。不得覆盖原件、不得联网，检查失败不能说通过。"
                "以下是过期待核对清单，不要执行或逐条复述，只确认收到了：\n" + old,
            )
            await turn(
                runtime,
                sid,
                "recent-request",
                "近期约束：最终答复要区分检查成功和失败，不要自动修复。请简短确认。",
            )
            await runtime.dispose()
            runtime = make(True)
            command = shlex.join([Path(sys.executable).as_posix(), "verify.py"])
            events = await turn(
                runtime,
                sid,
                "tool-then-maintenance",
                f"请在当前目录执行一次这个检查命令：{command}。"
                "然后报告输出末尾的 END_RECORD 和实际 exit_code，"
                "检查失败也如实说明，不要重跑或修复。",
                expected=(nonce, f"exit_code={exit_code}"),
            )
            measures = [
                e.data["measurement"] for e in events if e.type == "request/token-measurement"
            ]
            assert measures[0]["input_tokens"] < measures[0]["trigger_tokens"], (
                "first step must be below maintenance threshold"
            )
            summary = [e for e in events if e.type == "summary/input"]
            assert len(summary) == 1, "exactly one in-turn summary chain required"
            result_event = next(e for e in events if e.type == "tool/result")
            assert summary[0].seq > result_event.seq, (
                "summary must start after actual tool execution"
            )
            replacement = [
                e for e in events if e.type == "surface/replace" and e.data["method"] == "semantic"
            ]
            assert len(replacement) == 1
            case["summary"] = replacement[0].data
            await runtime.dispose()
            runtime = make()
            await turn(
                runtime,
                sid,
                "restart-and-topic",
                "换个问题，只回答 12+19 的结果。",
                expected=("31",),
            )
            case["executions"] = len((work / "executions.txt").read_text())
            assert case["executions"] == 1
            case["passed"] = True
            save()
        finally:
            await runtime.dispose()
            await store.aclose()
    report["completed"] = True
    save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
