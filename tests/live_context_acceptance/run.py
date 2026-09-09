"""Explicit E3 integrated real Provider journey; fresh directories, no pytest auto-dispatch."""

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from uuid import uuid4

from traceh.api.history import HistoryReadPolicy
from traceh.chat.activity import default_clock
from traceh.chat.driver import ChatDriver, TurnFailedUpdate
from traceh.cli.context_pressure import context_pressure_text
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


class ResponseGate:
    """Real response, held before delivery to exercise deterministic cancellation."""

    def __init__(self, provider):
        self.provider = provider
        self.name = provider.name
        self.hold = False
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self.returned_usage = []

    async def complete(self, request):
        response = await self.provider.complete(request)
        self.returned_usage.append(response.usage.to_dict())
        if self.hold:
            self.arrived.set()
            await self.release.wait()
        return response


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
                and argv[1] in {"verify.py", "huge.py"}
                and not (context.workspace / (argv[1] + ".executions")).exists()
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
    provider = ResponseGate(provider)
    report = {"model": model, "scripted_provider": False, "cases": [], "completed": False}

    def save():
        report["returned_usage"] = provider.returned_usage
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for exit_code in arguments.exit_codes:
        folder = root / f"exit-{exit_code}"
        work = folder / "workspace"
        work.mkdir(parents=True)
        nonce = uuid4().hex[:12]
        (work / "verify.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "with Path('verify.py.executions').open('a') as f: f.write('x')\n"
            "for i in range(1000): print(f'record {i}: validation observation complete')\n"
            f"print('END_RECORD={nonce}; exit_code={exit_code}')\nsys.exit({exit_code})\n",
            encoding="utf-8",
        )
        huge_nonce = uuid4().hex[:12]
        nearby_nonce = uuid4().hex[:12]
        (work / "huge.py").write_text(
            "from pathlib import Path\n"
            "with Path('huge.py.executions').open('a') as f: f.write('x')\n"
            "for i in range(6000):\n"
            " print(f'archive trace record {i}: original intact')\n"
            f" if i == 3000: print('EVIDENCE_ANCHOR={huge_nonce}')\n"
            f" if i == 3011: print('CHECK_DETAIL={nearby_nonce}')\n",
            encoding="utf-8",
        )
        case = {
            "exit_code": exit_code,
            "nonce": nonce,
            "huge_nonce": huge_nonce,
            "nearby_nonce": nearby_nonce,
            "turns": [],
        }
        report["cases"].append(case)
        store = SqliteEventStore(folder / "data" / "events")

        def make(maintenance=False, *, output_chars=50000, folder=folder, store=store):
            return build_default_runtime(
                RuntimeConfig(
                    data_dir=folder / "data",
                    provider=provider.name,
                    model=model,
                    max_steps=16,
                    max_output_tokens=3072,
                    max_tool_output_chars=output_chars,
                    context_input=ContextInputPolicy(
                        history_tier="directory",
                        total_bytes=30000,
                        history_bytes=24000,
                        item_bytes=16000,
                        max_query_bytes=200000,
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
                row["tool_calls"] = [e.data for e in events[start:] if e.type == "tool/call"]
                row["tool_results"] = [e.data for e in events[start:] if e.type == "tool/result"]
                row["contexts"] = [e.data for e in events[start:] if e.type == "context/input"]
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
            case["executions"] = len((work / "verify.py.executions").read_text())
            assert case["executions"] == 1
            # Same Session: locate a hidden original field after semantic replacement.
            candidate = old.splitlines()[37].split(": ", 1)[1]
            found = await turn(
                runtime,
                sid,
                "history-original",
                "核对最早那份过期待核对清单中第037项的 candidate 编码。"
                "请查压缩历史原文页，不要根据摘要猜，也不要读取工作区文件。",
                (candidate,),
            )
            assert any(
                e.type == "tool/call" and e.data["tool_name"] == "request_history_page"
                for e in found
            )
            await runtime.dispose()
            runtime = make(True, output_chars=1200)
            huge_command = shlex.join([Path(sys.executable).as_posix(), "huge.py"])
            found = await turn(
                runtime,
                sid,
                "retained-output-keyword",
                f"这是一项新的检查，尚未执行。先实际调用 shell 执行一次 {huge_command}。"
                "等新命令返回后，再找到新输出中 EVIDENCE_ANCHOR 的值。"
                "如果输出被保存，先关键词查找，再用 read_tool_output 读回命中后约二十行，"
                "一并回答附近 CHECK_DETAIL 的值；不要重跑命令，也不要读工作区文件。",
                (huge_nonce, nearby_nonce),
            )
            names = [e.data["tool_name"] for e in found if e.type == "tool/call"]
            assert "search_tool_output" in names and "read_tool_output" in names
            assert (work / "huge.py.executions").read_text() == "x"
            await runtime.dispose()
            runtime = make(True, output_chars=1200)
            await turn(runtime, sid, "restart-topic-again", "换题，只回答 23+18。", ("41",))

            # The network actually returns a response; cancellation wins before
            # that response can be delivered into the ordinary Step owner.
            updates = []

            async def consume(update, *, updates=updates):
                updates.append(update)

            driver = ChatDriver(
                runtime,
                sid,
                sink=consume,
                timeline=False,
                heartbeat_seconds=0,
                clock=default_clock(),
            )
            provider.hold = True
            provider.arrived.clear()
            usage_start = len(provider.returned_usage)
            running = asyncio.create_task(driver.run_turn("只回答收到，不要调用工具。"))
            await asyncio.wait_for(provider.arrived.wait(), timeout=120)
            await asyncio.gather(
                driver.cancel(reason="E3 real response delivery cancellation"),
                driver.cancel(reason="E3 repeated cancellation"),
            )
            cancelled = await asyncio.wait_for(running, timeout=20)
            assert cancelled.interrupted
            provider.hold = False
            case["cancellation"] = {
                "interrupted": True,
                "boundary": "real response received, before Runtime delivery",
                "returned_usage": provider.returned_usage[usage_start:],
            }
            assert not await runtime.check_invariants(sid)
            await turn(runtime, sid, "after-cancel", "只回答 16+17。", ("33",))
            updates.clear()
            outcome = await driver.run_turn(
                "待分析材料；先不要执行工具。\n" + "entry pending inspection " * 5000
            )
            assert outcome.failed
            failure = next(u for u in updates if isinstance(u, TurnFailedUpdate))
            assert failure.context_limit_exceeded and failure.context_pressure is not None
            case["refusal"] = {"display": context_pressure_text(failure.context_pressure)}
            await runtime.dispose()
            runtime = make()
            assert not await runtime.check_invariants(sid)
            assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
            case["all_attempts"] = [
                e.data
                for e in await runtime.sessions.read_session(sid)
                if e.type == "model/attempt-end"
            ]
            case["executions"] = {
                name: (work / (name + ".executions")).read_text()
                for name in ("verify.py", "huge.py")
            }
            assert all(v == "x" for v in case["executions"].values())
            case["passed"] = True
            save()
        finally:
            if "session_id" in case:
                case["all_attempts"] = [
                    e.data
                    for e in await runtime.sessions.read_session(case["session_id"])
                    if e.type == "model/attempt-end"
                ]
            save()
            await runtime.dispose()
            await store.aclose()
    report["completed"] = True
    save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exit-codes", type=int, nargs="+", choices=(0, 7), default=[0, 7])
    asyncio.run(run(parser.parse_args()))
