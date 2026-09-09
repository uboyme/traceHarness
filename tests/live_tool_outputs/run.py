"""Explicit real-provider acceptance. Never imported or run by pytest collection.

Usage: python tests/live_tool_outputs/run.py --profile PATH --output NEW_DIRECTORY
Credentials are loaded through existing configuration; reports contain no keys.
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from traceh.api.history import HistoryReadPolicy
from traceh.cli.credentials import load_key
from traceh.cli.main import (
    _configure_from_environment,
    _provider_and_model,
    build_parser,
)
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
    name = "isolated-output-acceptance"

    async def check(self, call, tool, context):
        allowed = call.name in {
            "read_tool_output",
            "list_tool_outputs",
            "search_tool_output",
            "request_history_page",
            "list_files",
            "read_file",
            "search_text",
        }
        if call.name == "shell":
            argv = shlex.split(call.arguments.get("command", ""))
            allowed = (
                len(argv) == 2
                and Path(argv[0]).name.lower() in {"python", "python.exe"}
                and argv[1] in {"telemetry.py", "./telemetry.py", ".\\telemetry.py"}
                and not (context.workspace / "executions.txt").exists()
            )
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "Workspace reads, one fixture execution, and historical output reads are allowed.",
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
        ]
    )
    args.tui_explicit = {"auto_compact"}
    args, error = initial_settings(args)
    if error:
        raise RuntimeError("live-profile-invalid")
    environment = dict(os.environ)
    _configure_from_environment(args, environment=environment)
    args.tui_api_key = environment.get(args.api_key_env) or load_key(args)
    provider, model = _provider_and_model(args)
    provider.timeout_seconds = 45
    retry_policy = ModelRetryPolicy(6, 360, 1, 5, 5, 0)
    policy = ContextInputPolicy(
        history_tier="directory",
        total_bytes=30000,
        history_bytes=24000,
        item_bytes=16000,
        max_blocks=8,
        max_exclusions=12,
        max_query_bytes=6000,
        history=HistoryReadPolicy(
            max_blocks=8,
            max_depth=16,
            page_bytes=16000,
            page_messages=24,
            max_source_events=5000,
            max_source_bytes=8000000,
            max_requests=16,
        ),
    )
    report = {
        "model": model,
        "scripted_provider": False,
        "cases": [],
        "completed": False,
        "keyword_search": arguments.keyword_search,
        "fold_tools": arguments.fold_tools,
        "token_meter": arguments.token_meter,
        "retry_max_attempts": retry_policy.max_attempts,
        "provider_timeout_seconds": provider.timeout_seconds,
    }

    def save():
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def turn(runtime, sid, case, label, question, expected=(), *, record_index=None):
        before = len(await runtime.sessions.read_session(sid))
        row = {"label": label, "question": question}
        case["turns"].append(row)
        save()
        try:
            result = await runtime.run_existing(sid, question)
            fresh = (await runtime.sessions.read_session(sid))[before:]
            if record_index is not None:
                effects = await runtime.sessions.read_effects(sid)
                output = next(
                    e.data["retained_output"]
                    for e in effects
                    if e.type == "effect/outcome"
                    and e.data.get("tool_name") == "shell"
                    and "retained_output" in e.data
                )
                indices = (record_index,) if isinstance(record_index, int) else record_index
                records = output["data"]["stdout"]
                expected = []
                for index in indices:
                    record = json.loads(
                        records.split("\n\n")[index].splitlines()[-1]
                        if arguments.keyword_search
                        else records.splitlines()[index]
                    )
                    expected.extend((record["设备标签"], record["复核码"]))
            row.update(
                answer=result.final_text,
                steps=result.steps,
                tool_results=[
                    {
                        "tool": e.data["tool_name"],
                        "status": e.data["status"],
                        "call_id": e.data["tool_call_id"],
                    }
                    for e in fresh
                    if e.type == "tool/result"
                ],
                read_offsets=[
                    json.loads(e.data["content"])["offset"]
                    for e in fresh
                    if e.type == "tool/result"
                    and e.data["tool_name"] == "read_tool_output"
                    and e.data["status"] == "succeeded"
                ],
                expected_found=all(value in result.final_text for value in expected),
                tool_calls=[
                    {"tool": e.data["tool_name"], "arguments": e.data["arguments"]}
                    for e in fresh
                    if e.type == "tool/call"
                ],
                search_pages=[
                    json.loads(e.data["content"])
                    for e in fresh
                    if e.type == "tool/result"
                    and e.data["tool_name"] == "search_tool_output"
                    and e.data["status"] == "succeeded"
                ],
                token_measurements=[
                    e.data["measurement"] for e in fresh if e.type == "request/token-measurement"
                ],
                attempt_usage=[e.data.get("usage") for e in fresh if e.type == "model/attempt-end"],
            )
            assert row["expected_found"], "live-answer-missing-source-fact"
            assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
            assert not runtime.invariants.check(
                await runtime.sessions.read_session(sid), await runtime.sessions.read_effects(sid)
            )
            row["replay_and_invariants"] = True
        except Exception as error:
            row["error_type"] = type(error).__name__
            fresh = (await runtime.sessions.read_session(sid))[before:]
            row["partial_tool_results"] = [
                {"tool": e.data["tool_name"], "status": e.data["status"]}
                for e in fresh
                if e.type == "tool/result"
            ]
            row["failure_code"] = getattr(error, "code", None)
            raise
        finally:
            save()
            print(
                label,
                json.dumps(
                    {
                        key: row[key]
                        for key in ("answer", "expected_found", "error_type")
                        if key in row
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    for exit_code in (0, 7):
        folder = root / f"exit-{exit_code}"
        workspace = folder / "workspace"
        workspace.mkdir(parents=True)
        # Values exist only after real child execution, never in the script source.
        script = (
            "from pathlib import Path\nfrom uuid import uuid4\nimport json,sys\n"
            'p=Path("executions.txt")\np.write_text(p.read_text()+"x" if p.exists() else "x")\n'
            "for i in range(260):\n"
            ' r={"记录":f"R{i:04}","设备标签":f"仪表-{uuid4().hex[:10]}",'
            '"复核码":uuid4().hex[:12],"备注":"本次诊断的原始随机观测数据。"}\n'
            ' sys.stdout.buffer.write((json.dumps(r,ensure_ascii=False)+"\\n").encode("utf-8"))\n'
            f"sys.exit({exit_code})\n"
        )
        if arguments.keyword_search:
            script = (
                "from pathlib import Path\nfrom uuid import uuid4\nimport json,sys\n"
                'p=Path("executions.txt")\np.write_text(p.read_text()+"x" if p.exists() else "x")\n'
                "for i in range(260):\n"
                ' marker=" 复检" if i in (78, 207) else ""\n'
                ' lines=[f"记录 R{i:04}{marker}"]\n'
                ' lines += [f"采样阶段 {j}：等待设备返回观测；本行没有最终核验结果。"'
                " for j in range(9)]\n"
                ' record={"设备标签":f"仪表-{uuid4().hex[:10]}","复核码":uuid4().hex[:12]}\n'
                " lines.append(json.dumps(record,ensure_ascii=False))\n"
                ' sys.stdout.buffer.write(("\\n".join(lines)+"\\n\\n").encode("utf-8"))\n'
                f"sys.exit({exit_code})\n"
            )
        (workspace / "telemetry.py").write_text(script, encoding="utf-8")
        case = {"exit_code": exit_code, "turns": []}
        report["cases"].append(case)

        def make(folder=folder, case=case, *, fold_threshold=None):
            store = SqliteEventStore(folder / "data" / "events")
            runtime = build_default_runtime(
                RuntimeConfig(
                    data_dir=folder / "data",
                    provider=provider.name,
                    model=model,
                    max_steps=45,
                    max_tool_output_chars=4096,
                    context_input=policy,
                    model_retry_policy=retry_policy,
                    token_budget=(
                        TokenBudgetPolicy(
                            "cl100k_base",
                            case["fold_input_limit"] + 2048
                            if fold_threshold is not None
                            else 100_000,
                            1024,
                            1024,
                        )
                        if arguments.token_meter
                        else None
                    ),
                    compaction=(
                        CompactionPolicy(True, fold_threshold, 1200, 1)
                        if fold_threshold is not None
                        else None
                    ),
                ),
                provider=provider,
                event_store=store,
                policies=(FixturePolicy(),),
            )
            return runtime, store

        runtime, store = make()
        try:
            sid = await runtime.create_session(workspace)
            case["session_id"] = sid
            await turn(
                runtime,
                sid,
                case,
                "read-new-output",
                "请运行一次 python telemetry.py，查一下日志中 R0164 的设备标签和复核码。"
                "另外告诉我程序的退出码。请依据实际执行结果回答。",
                record_index=164,
            )
            assert case["turns"][-1]["read_offsets"] or case["turns"][-1]["search_pages"], (
                "live-did-not-access-retained-output"
            )
            if arguments.keyword_search:
                assert case["turns"][-1]["search_pages"], "live-did-not-search"
                order = [call["tool"] for call in case["turns"][-1]["tool_calls"]]
                assert order.index("search_tool_output") < order.index("read_tool_output")
            fold_threshold = None
            if arguments.fold_tools:
                await turn(
                    runtime,
                    sid,
                    case,
                    "recent-conversation",
                    "先停一下查询。请只回答：好的。",
                    ("好的",),
                )
                before_fold = runtime.surface.project(await runtime.sessions.read_session(sid))
                fold_threshold = surface_utf8_bytes(before_fold) - 1
                case["before_fold_bytes"] = fold_threshold + 1
                if arguments.token_meter:
                    case["fold_input_limit"] = (
                        case["turns"][-1]["token_measurements"][-1]["input_tokens"] + 2048
                    )
            else:
                events = await runtime.sessions.read_session(sid)
                await runtime.compaction.replace_through(
                    sid,
                    through_seq=events[-1].seq,
                    summary="本会话执行过一份仪表诊断日志。具体观测保留在历史工具输出中。",
                )
        finally:
            await runtime.dispose()
            await store.aclose()
            save()
        runtime, store = make(fold_threshold=fold_threshold)
        try:
            await turn(
                runtime,
                sid,
                case,
                "restart-after-compaction",
                "刚才那份日志里 R0239 的设备标签和复核码是什么？请查当时的记录。",
                record_index=239,
            )
            assert case["turns"][-1]["read_offsets"] or case["turns"][-1]["search_pages"], (
                "live-resume-did-not-access-original"
            )
            if arguments.keyword_search:
                assert case["turns"][-1]["search_pages"], "live-resume-did-not-search"
                order = [call["tool"] for call in case["turns"][-1]["tool_calls"]]
                assert order.index("search_tool_output") < order.index("read_tool_output")
            if arguments.fold_tools:
                events = await runtime.sessions.read_session(sid)
                folds = [e for e in events if e.type == "surface/replace"]
                assert len(folds) == 1 and folds[0].data["method"] == "tool-fold"
                # Token mode prepares the current input before selecting old history.
                # Compare both projections around the actual fold, at the same scope.
                before_fold = runtime.surface.project(events, through_seq=folds[0].seq - 1)
                case["before_fold_bytes"] = surface_utf8_bytes(before_fold)
                folded = runtime.surface.project(events, through_seq=folds[0].seq)
                assert len(folded) == len(before_fold)
                for old, new in zip(before_fold, folded, strict=True):
                    if old != new:
                        assert old.role == new.role == "tool"
                        assert old.tool_call_id == new.tool_call_id
                        assert old.name == new.name == "shell"
                        assert (
                            json.loads(old.content)["output_ref"]
                            == json.loads(new.content)["output_ref"]
                        )
                case["after_fold_bytes"] = surface_utf8_bytes(folded)
                assert case["after_fold_bytes"] < fold_threshold
                case["fold_event"] = folds[0].data
                await runtime.dispose()
                await store.aclose()
                runtime, store = make()  # Reopen the durable fold with automatic maintenance off.
                await turn(
                    runtime,
                    sid,
                    case,
                    "restart-after-tool-fold",
                    "再查一下同一份日志 R0183 的设备标签和复核码。",
                    record_index=183,
                )
                assert case["turns"][-1]["search_pages"] or case["turns"][-1]["read_offsets"]
            else:
                assert any(
                    r["tool"] in {"request_history_page", "list_tool_outputs"}
                    for r in case["turns"][-1]["tool_results"]
                ), "live-source-directory-not-consulted"
            if arguments.keyword_search:
                await turn(
                    runtime,
                    sid,
                    case,
                    "repeated-keyword",
                    "这份日志中标了复检的记录有哪些？列出每条的设备标签和复核码。",
                    record_index=(78, 207),
                )
                pages = case["turns"][-1]["search_pages"]
                assert pages and sum(len(p["matches"]) for p in pages) >= 2
                await turn(
                    runtime,
                    sid,
                    case,
                    "absent-keyword",
                    "这份日志有没有 R9999 这条记录？没有就明确告诉我没找到，不要猜内容。",
                )
                row = case["turns"][-1]
                assert any(p["query"] == "R9999" and not p["matches"] for p in row["search_pages"])
                assert any(
                    word in row["answer"].lower()
                    for word in ("没有", "未找到", "没找到", "不存在", "not found", "no record")
                )
            await turn(
                runtime,
                sid,
                case,
                "new-topic",
                "换个话题：23 加 19 等于多少？只回答数字。",
                ("42",),
            )
            assert (workspace / "executions.txt").read_text() == "x"
            case["execution_count"] = 1
            effects = await runtime.sessions.read_effects(sid)
            retained = [e.data["output_ref"] for e in effects if "retained_output" in e.data]
            case["retained_outputs"] = retained
            attempts = [
                e for e in await runtime.sessions.read_session(sid) if e.type == "model/attempt-end"
            ]
            case["attempts"] = len(attempts)
            case["usage"] = [e.data.get("usage") for e in attempts]
            case["passed"] = True
        finally:
            await runtime.dispose()
            await store.aclose()
            save()
    report["completed"] = True
    save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--keyword-search", action="store_true")
    parser.add_argument("--fold-tools", action="store_true")
    parser.add_argument("--token-meter", action="store_true")
    asyncio.run(run(parser.parse_args()))
