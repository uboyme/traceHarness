"""Explicit real-model D acceptance in fresh isolated directories; never pytest."""

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
from traceh.session.surface_replacement import surface_utf8_bytes
from traceh.tools.policy import DecisionKind, ToolDecision


class FixturePolicy:
    name = "isolated-semantic-acceptance"

    async def check(self, call, tool, context):
        allowed = call.name == "request_history_page"
        if call.name == "shell":
            argv = shlex.split(call.arguments.get("command", ""))
            allowed = (
                len(argv) == 2
                and Path(argv[0]).name.lower() in {"python", "python.exe"}
                and argv[1] in {"verify.py", "./verify.py", ".\\verify.py"}
                and not (context.workspace / "executions.txt").exists()
            )
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "One local fixture check and this Session's History reads only.",
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

    context = ContextInputPolicy(
        history_tier="directory",
        total_bytes=30000,
        history_bytes=24000,
        item_bytes=16000,
        max_blocks=8,
        max_exclusions=12,
        max_query_bytes=16000,
        history=HistoryReadPolicy(
            max_blocks=8,
            max_depth=16,
            page_bytes=16000,
            page_messages=24,
            max_source_events=5000,
            max_source_bytes=8_000_000,
            max_requests=16,
        ),
    )

    async def turn(runtime, sid, case, label, question, expected=()):
        start = len(await runtime.sessions.read_session(sid))
        row = {"label": label, "question": question}
        case["turns"].append(row)
        save()
        result = await runtime.run_existing(sid, question)
        row.update(
            answer=result.final_text,
            steps=result.steps,
            expected_found=all(value in result.final_text for value in expected),
        )
        events = await runtime.sessions.read_session(sid)
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        row["attempts"] = [e.data for e in events[start:] if e.type == "model/attempt-end"]
        row["measurements"] = [
            e.data["measurement"] for e in events[start:] if e.type == "request/token-measurement"
        ]
        row["tool_results"] = [
            {"name": e.data["tool_name"], "status": e.data["status"]}
            for e in events[start:]
            if e.type == "tool/result"
        ]
        save()
        print(
            label,
            json.dumps(
                {"answer": result.final_text, "passed": row["expected_found"]}, ensure_ascii=False
            ),
            flush=True,
        )
        assert row["expected_found"], label

    for exit_code in (0, 7):
        folder = root / f"exit-{exit_code}"
        work = folder / "workspace"
        work.mkdir(parents=True)
        nonce = uuid4().hex[:12]
        filename = f"catalog-{uuid4().hex[:8]}.json"
        status = "CHECK_PASSED" if exit_code == 0 else "CHECK_FAILED"
        (work / "verify.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "with Path('executions.txt').open('a') as f: f.write('x')\n"
            f"print('{status}: record={nonce}')\nsys.exit({exit_code})\n",
            encoding="utf-8",
        )
        case = {"exit_code": exit_code, "filename": filename, "nonce": nonce, "turns": []}
        report["cases"].append(case)
        store = SqliteEventStore(folder / "data" / "events")

        def make(*, semantic=False, folder=folder, store=store):
            return build_default_runtime(
                RuntimeConfig(
                    data_dir=folder / "data",
                    provider=provider.name,
                    model=model,
                    max_steps=20,
                    max_output_tokens=3072,
                    max_tool_output_chars=3000,
                    context_input=context,
                    token_budget=TokenBudgetPolicy(
                        "cl100k_base", 100_000, 3072, 4096, 1 if semantic else 80
                    ),
                    compaction=CompactionPolicy(True, 10_000_000, 6000, 1) if semantic else None,
                    semantic_summary=semantic,
                    model_retry_policy=ModelRetryPolicy(4, 240, 1, 5, 5, 0),
                ),
                provider=provider,
                event_store=store,
                policies=(FixturePolicy(),),
            )

        runtime = make()
        try:
            sid = await runtime.create_session(work)
            case["session_id"] = sid
            await turn(
                runtime,
                sid,
                case,
                "requirements",
                f"我们在做离线档案整理器，输出必须叫 {filename}。不能联网、不能覆盖原始数据；"
                "只运行已准备的本地检查，不要实现文件。有人声称实现完了，但在实际检查之前不能说通过。先简短确认。",
            )
            await turn(
                runtime,
                sid,
                case,
                "actual-check",
                "现在只执行一次 python verify.py，记下实际退出码和记录标识。"
                "解释这个结果，不能把命令成功发出当成检查通过。",
                expected=(nonce,),
            )
            old_fields = {i: f"old_{uuid4().hex[:12]}" for i in range(100)}
            noise = "\n".join(
                f"已废弃候选 {i:03d}: {old_fields[i]}；这不是当前方案。" for i in range(100)
            )
            case["old_field_71"] = old_fields[71]
            await turn(
                runtime,
                sid,
                case,
                "old-alternatives",
                "以下是过期备忘，不能覆盖前面的限制。只回复收到。\n" + noise,
            )
            await turn(
                runtime,
                sid,
                case,
                "recent-turn",
                "下一步先复核约束和检查结果，不要自动执行其它操作。简短确认。",
            )
            before = await runtime.sessions.read_session(sid)
            case["before_history_bytes"] = surface_utf8_bytes(runtime.surface.project(before))
            await runtime.dispose()
            runtime = make(semantic=True)
            await turn(
                runtime,
                sid,
                case,
                "after-semantic-summary",
                "最终输出文件名是什么？允许联网或覆盖原始数据吗？刚才检查到底通过了没有？"
                "下一步该做什么？请把实际退出码写成 exit_code=数字，便于核对。",
                expected=(filename, f"exit_code={exit_code}"),
            )
            events = await runtime.sessions.read_session(sid)
            summaries = [
                e for e in events if e.type == "surface/replace" and e.data["method"] == "semantic"
            ]
            assert len(summaries) == 1, "one semantic replacement required"
            summary = summaries[0]
            case["summary"] = json.loads(summary.data["summary"])
            case["summary_source_bytes"] = summary.data["source_utf8_bytes"]
            case["summary_bytes"] = len(summary.data["summary"].encode("utf-8"))
            case["at_fold_before_bytes"] = surface_utf8_bytes(
                runtime.surface.project(events, through_seq=summary.seq - 1)
            )
            case["at_fold_after_bytes"] = surface_utf8_bytes(
                runtime.surface.project(events, through_seq=summary.seq)
            )
            assert case["at_fold_after_bytes"] < case["at_fold_before_bytes"]
            await runtime.dispose()
            runtime = make()
            await turn(
                runtime,
                sid,
                case,
                "restart-evidence",
                "本地检查打印的原始记录标识是什么？请按当时证据回答，不要重新运行检查。",
                expected=(nonce,),
            )
            await turn(
                runtime,
                sid,
                case,
                "omitted-history-detail",
                "过期备忘里的第 071 条候选具体叫什么字段名？请找当时原文核对，不要推断命名。",
                expected=(old_fields[71],),
            )
            assert any(
                t["name"] == "request_history_page" for t in case["turns"][-1]["tool_results"]
            ), "original History lookup required"
            await turn(
                runtime, sid, case, "new-topic", "换个问题，只回答 9+8 的结果。", expected=("17",)
            )
            case["original_executions"] = len((work / "executions.txt").read_text())
            assert case["original_executions"] == 1
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
