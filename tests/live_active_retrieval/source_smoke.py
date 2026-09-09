"""Explicit AR-C diagnostic, real Provider; never part of the scored AR-D grid."""

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from skill_fixtures import contribution, digest, policy
from test_memory_context import memory_case

from live_active_retrieval.history_smoke import file_digest, load_provider
from traceh.api.skills import SkillSection, SkillSectionContent
from traceh.llm.retry import ModelRetryPolicy
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.tools.policy import DecisionKind, ToolDecision


class ReferenceOnly:
    name = "ar-c-isolated-reference-source"

    def __init__(self, kind):
        self.allowed = {
            "search_" + kind,
            {"memory": "request_workspace_memory", "skill": "request_skill_reference"}[kind],
        }

    async def check(self, call, tool, context):
        return ToolDecision(
            DecisionKind.ALLOW if call.name in self.allowed else DecisionKind.DENY,
            "This task reads the configured reference source, not workspace files.",
            self.name,
        )


async def run(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider, model = load_provider(args.profile)
    config = dict(
        provider=provider.name,
        model=model,
        temperature=0,
        max_output_tokens=2048,
        token_budget=TokenBudgetPolicy("cl100k_base", 32000, 2048, 2048),
        model_retry_policy=ModelRetryPolicy(3, 180, 1, 5, 5, 0),
    )
    report = {
        "purpose": "AR-C diagnostic, not AR-D score",
        "provider": provider.name,
        "model": model,
        "runner_sha256": file_digest(Path(__file__)),
        "cases": [],
        "complete": False,
        "scripted_provider": False,
    }

    def save():
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()

    async def check(runtime, session, folder, question, expected):
        case = {
            "kind": folder.name,
            "session_id": session,
            "question": question,
            "expected": expected,
        }
        report["cases"].append(case)
        save()
        try:
            result = await runtime.run_existing(session, question)
            case.update(answer=result.final_text, steps=result.steps, reason=result.reason)
        except Exception as error:
            case["error_type"] = type(error).__name__
        finally:
            events = await runtime.sessions.read_session(session)
            (folder / "source-events.json").write_text(
                json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            case["replay_errors"] = list(
                await verify_request_snapshots(runtime.sessions, runtime.surface, session)
            )
            case["invariant_errors"] = [str(v) for v in await runtime.check_invariants(session)]
            case["evidence_seen"] = any(
                expected in b["body"]
                for e in events
                if e.type == "context/input"
                for b in e.data["blocks"]
                if b["tier"] in {"search", "section", "chunk"}
            )
            case["search_used"] = any(
                e.type == "tool/result"
                and e.data["tool_name"] == "search_" + folder.name
                and e.data["status"] == "succeeded"
                for e in events
            )
            case["attempts"] = [e.data for e in events if e.type == "model/attempt-end"]
            case["passed"] = (
                expected in case.get("answer", "")
                and case["evidence_seen"]
                and not case["replay_errors"]
                and not case["invariant_errors"]
            )
            save()
            print(
                json.dumps(
                    {
                        k: case.get(k)
                        for k in ("kind", "answer", "steps", "search_used", "passed", "error_type")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    folder = root / "memory"
    folder.mkdir()
    nonce = "MAPLE-" + uuid4().hex[:8]
    body = "已经批准的值班联络口令是 " + nonce + "。"
    (folder / "fixture.json").write_text(
        json.dumps({"body": body}, ensure_ascii=False), encoding="utf-8"
    )
    async with memory_case(
        folder,
        provider=provider,
        tier="directory",
        body=body,
        max_steps=12,
        config_changes=config,
        runtime_options={"policies": (ReferenceOnly("memory"),)},
    ) as (runtime, _, _, session, _, _):
        await check(runtime, session, folder, "项目已批准的值班联络口令是什么？", nonce)

    folder = root / "skill"
    folder.mkdir()
    nonce = "WILLOW-" + uuid4().hex[:8]
    body = "设备维护手册规定，终止作业的无线电口令是 " + nonce + "。"
    value = contribution("reference.author", "maintenance.handbook", "placeholder")
    entries = [
        (f"part-{i:02}", "普通器材清点", "说明清点顺序。", "清点常用器材。") for i in range(31)
    ]
    entries[23] = ("part-23", "无线电终止作业", "停止维护作业时使用的无线电口令。", body)
    value = replace(
        value,
        descriptor=replace(
            value.descriptor,
            title="设备维护手册",
            summary="设备维护的现场操作说明。",
            tags=(),
            sections=tuple(
                SkillSection(
                    sid,
                    "section",
                    digest(text),
                    len(text.encode("utf-8")),
                    title=title,
                    summary=summary,
                )
                for sid, title, summary, text in entries
            ),
        ),
        sections=tuple(SkillSectionContent(sid, text) for sid, _, _, text in entries),
    )
    (folder / "fixture.json").write_text(
        json.dumps(
            {
                "descriptor": value.descriptor.to_dict(),
                "sections": [{"id": sid, "body": text} for sid, _, _, text in entries],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    workspace = folder / "workspace"
    workspace.mkdir()
    runtime, store, _, session, _ = await build_case(
        workspace,
        provider=provider,
        values=(value,),
        activation_policy=policy(max_catalog_bytes=80000),
        context=context_policy(
            item_bytes=2500,
            max_blocks=6,
            skills=retrieval_policy(
                default_tier="summary",
                max_catalog_bytes=80000,
                max_corpus_items=80,
                max_corpus_bytes=200000,
            ),
        ),
        config_changes={**config, "max_steps": 12},
        runtime_options={"policies": (ReferenceOnly("skill"),)},
    )
    try:
        await select(runtime, session, value)
        await check(
            runtime, session, folder, "设备维护手册里，终止作业时的无线电口令是什么？", nonce
        )
    finally:
        await runtime.dispose()
        await store.aclose()
    report["complete"] = True
    save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(run(parser.parse_args()))
