"""E2 real Provider reads; explicit fixture content, original Runtime/authority owners.

Run with tests on PYTHONPATH. Fixture helpers supply local plugin contributions
and host-approved test facts; they never script model responses or Tool calls.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from memory_fixtures import memory_policy
from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from skill_fixtures import digest, policy
from test_memory_context import memory_case
from test_skill_navigation import navigation_skill

from traceh.api.history import HistoryReadPolicy
from traceh.api.skills import SkillSectionContent
from traceh.cli.credentials import load_key
from traceh.cli.main import _configure_from_environment, _provider_and_model, build_parser
from traceh.cli.tui_entry import initial_settings
from traceh.llm.retry import ModelRetryPolicy
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision


class ReferenceOnly:
    name = "isolated-token-reference-acceptance"

    async def check(self, call, tool, context):
        allowed = call.name in {
            "request_skill_reference",
            "request_workspace_memory",
            "request_history_page",
        }
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "Read the disclosed reference only; no workspace or process effects.",
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

    async def check(runtime, session, case, question, expected_body, nonce):
        case["session_id"] = session
        case["question"] = question
        save()
        before = len(await runtime.sessions.read_session(session))
        try:
            result = await runtime.run_existing(session, question)
            case.update(answer=result.final_text, steps=result.steps, reason=result.reason)
        finally:
            events = await runtime.sessions.read_session(session)
            case["attempts"] = [e.data for e in events[before:] if e.type == "model/attempt-end"]
            case["contexts"] = [e.data for e in events[before:] if e.type == "context/input"]
            case["measurements"] = [
                e.data["measurement"]
                for e in events[before:]
                if e.type == "request/token-measurement"
            ]
            case["tool_results"] = [e.data for e in events[before:] if e.type == "tool/result"]
            save()
        case["replay_errors"] = list(
            await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        )
        case["invariant_errors"] = list(await runtime.check_invariants(session))
        bodies = [
            b for c in case["contexts"] for b in c["blocks"] if b["tier"] in {"section", "chunk"}
        ]
        case["full_body_seen"] = any(
            nonce in b["body"] if case["kind"] == "history" else b["body"] == expected_body
            for b in bodies
        )
        case["token_excluded"] = any(
            e["reason"] == "token-budget-excluded"
            for c in case["contexts"]
            for e in c["exclusions"]
        )
        case["passed"] = (
            not case["replay_errors"]
            and not case["invariant_errors"]
            and case["reason"] == "completed"
            and any(t["status"] == "succeeded" for t in case["tool_results"])
            and all(
                t["tool_name"]
                in {"request_skill_reference", "request_workspace_memory", "request_history_page"}
                for t in case["tool_results"]
            )
            and (case["full_body_seen"] == case["wide"])
            and (nonce in case["answer"] if case["wide"] else nonce not in case["answer"])
            and (not case["token_excluded"] if case["wide"] else case["token_excluded"])
        )
        save()
        print(
            json.dumps(
                {
                    k: case[k]
                    for k in ("kind", "wide", "passed", "steps", "answer", "token_excluded")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert case["passed"], "real-reference-token-acceptance-failed"

    for kind in arguments.kinds:
        for wide in (True, False):
            folder = root / f"{kind}-{'wide' if wide else 'narrow'}"
            folder.mkdir()
            nonce = uuid4().hex[:12]
            body = (
                "温室核验约束。以下为原始记录，最终核验标识位于末尾。\n"
                + (
                    "inspection observation: verify the declared condition before acceptance.\n"
                    * 800
                )
                + f"最终核验标识：{nonce}。"
            )
            case = {"kind": kind, "wide": wide, "nonce": nonce}
            report["cases"].append(case)
            config = {
                "provider": provider.name,
                "model": model,
                "max_output_tokens": 2048,
                "token_budget": TokenBudgetPolicy(
                    "cl100k_base", 40000 if wide else 11000, 2048, 1024
                ),
                "model_retry_policy": ModelRetryPolicy(4, 240, 1, 5, 5, 0),
            }
            case["token_policy"] = config["token_budget"].to_dict()
            (folder / "expected-body.txt").write_text(body, encoding="utf-8")
            if kind == "skill":
                value = navigation_skill()
                section = replace(
                    value.descriptor.sections[0],
                    title="核验约束",
                    summary="完整核验记录及末尾的核验标识。",
                    content_digest=digest(body),
                    content_bytes=len(body.encode()),
                )
                value = replace(
                    value,
                    descriptor=replace(
                        value.descriptor,
                        title="温室核验规程",
                        summary="温室核验约束和最终核验标识的查询说明。",
                        sections=(section,),
                        resources=(),
                    ),
                    sections=(SkillSectionContent(section.section_id, body),),
                )
                runtime, store, _, session, _ = await build_case(
                    folder,
                    provider=provider,
                    values=(value,),
                    activation_policy=policy(max_content_bytes=100000),
                    context=context_policy(
                        total_bytes=160000,
                        item_bytes=100000,
                        skills=retrieval_policy(default_tier="directory", context_bytes=100000),
                    ),
                    config_changes={**config, "max_steps": 8},
                    runtime_options={"policies": (ReferenceOnly(),)},
                )
                try:
                    await select(runtime, session, value)
                    await runtime.skill_context.rebuild_index(session)
                    await check(
                        runtime,
                        session,
                        case,
                        "请根据温室核验规程，告诉我最终核验标识。需要查正文；如果拿不到正文，请明确说明资料不足，不要猜测，也不要读工作区文件。",
                        body,
                        nonce,
                    )
                finally:
                    await runtime.dispose()
                    await store.aclose()
            elif kind == "memory":
                async with memory_case(
                    folder,
                    provider=provider,
                    tier="directory",
                    max_steps=8,
                    body=body,
                    authority_policy=memory_policy(max_body_bytes=100000),
                    overrides={
                        "total_bytes": 160000,
                        "item_bytes": 100000,
                        "memory": retrieval_policy(
                            default_tier="directory", context_bytes=100000, max_corpus_bytes=500000
                        ),
                    },
                    config_changes=config,
                ) as (runtime, _, _, session, _, _):
                    await runtime.memory.rebuild_index(session)
                    await check(
                        runtime,
                        session,
                        case,
                        "请查已批准的项目记忆：温室核验约束中的最终核验标识是什么？需要查正文；如果拿不到正文，请明确说明资料不足，不要猜测，也不要读工作区文件。",
                        body,
                        nonce,
                    )
            else:
                context = context_policy(
                    skills=None,
                    history_tier="directory",
                    total_bytes=160000,
                    history_bytes=120000,
                    item_bytes=100000,
                    max_query_bytes=100000,
                    history=HistoryReadPolicy(
                        max_blocks=8,
                        max_depth=16,
                        page_bytes=160000,
                        page_messages=24,
                        max_source_events=5000,
                        max_source_bytes=2_000_000,
                        max_requests=12,
                    ),
                )
                store = SqliteEventStore(folder / "events")
                runtime = build_default_runtime(
                    RuntimeConfig(
                        data_dir=folder / "data",
                        context_input=context,
                        **{**config, "token_budget": None},
                    ),
                    provider=provider,
                    event_store=store,
                    policies=(ReferenceOnly(),),
                )
                try:
                    session = await runtime.create_session(folder)
                    await runtime.run_existing(
                        session, "以下是旧交接记录，只简短确认收到，不复述任何标识：\n" + body
                    )
                    events = await runtime.sessions.read_session(session)
                    case["bootstrap_attempts"] = [
                        e.data for e in events if e.type == "model/attempt-end"
                    ]
                    await runtime.compaction.replace_through(
                        session,
                        through_seq=events[-1].seq,
                        summary="此前提供过温室核验约束的旧交接记录，细节需查历史原文。",
                    )
                    await runtime.dispose()
                    runtime = build_default_runtime(
                        RuntimeConfig(
                            data_dir=folder / "data", context_input=context, max_steps=8, **config
                        ),
                        provider=provider,
                        event_store=store,
                        policies=(ReferenceOnly(),),
                    )
                    await check(
                        runtime,
                        session,
                        case,
                        "请找回本会话之前旧交接记录中的最终核验标识。请查历史原文，拿不到就说明资料不足，不要猜测或读取工作区文件。",
                        body,
                        nonce,
                    )
                finally:
                    await runtime.dispose()
                    await store.aclose()
    report["completed"] = True
    save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=("skill", "memory", "history"),
        default=["skill", "memory", "history"],
    )
    asyncio.run(run(parser.parse_args()))
