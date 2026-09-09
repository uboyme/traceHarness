"""AR-B diagnostic real History journeys; separate from the frozen AR-D score set."""

import argparse
import asyncio
import hashlib
import json
import os
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
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision


class HistoryOnly:
    name = "ar-b-isolated-diagnostic"

    async def check(self, call, tool, context):
        return ToolDecision(
            DecisionKind.ALLOW
            if call.name in {"search_history", "request_history_page"}
            else DecisionKind.DENY,
            "This isolated historical question has no filesystem or process effects.",
            self.name,
        )


def load_provider(profile):
    args = build_parser().parse_args(
        [
            "chat",
            "--tui",
            "--tui-profile",
            str(profile),
            "--auto-compact",
            "off",
            "--auto-compact-method",
            "extractive",
        ]
    )
    args.tui_explicit = {"auto_compact", "auto_compact_method"}
    args, error = initial_settings(args)
    if error:
        raise RuntimeError("authorized-profile-unavailable")
    environment = dict(os.environ)
    _configure_from_environment(args, environment=environment)
    args.tui_api_key = environment.get(args.api_key_env) or load_key(args)
    provider, model = _provider_and_model(args)
    provider.timeout_seconds = 60
    return provider, model


async def run(arguments):
    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider, model = load_provider(arguments.profile)
    config = ContextInputPolicy(
        history_tier="directory",
        total_bytes=30000,
        history_bytes=18000,
        item_bytes=6000,
        max_blocks=6,
        max_exclusions=40,
        max_query_bytes=512,
        history=HistoryReadPolicy(
            max_blocks=16,
            max_depth=16,
            page_bytes=1600,
            page_messages=4,
            max_source_events=4000,
            max_source_bytes=4000000,
            max_requests=8,
        ),
    )
    report = {
        "purpose": "AR-B diagnostic; not AR-D scored evidence",
        "provider": provider.name,
        "model": model,
        "scripted_provider": False,
        "cases": [],
        "complete": False,
        "runner_sha256": file_digest(Path(__file__)),
        "context_policy": config.to_dict(),
    }
    nonce = "ORCHID-" + uuid4().hex[:8]
    fixture = {
        "zh": {
            "source": f"设备维修组临时联络代号是 {nonce}。",
            "question": "之前维修组用哪个临时联络代号来着？",
        },
        "en": {
            "source": f"The temporary repair team contact code is {nonce}.",
            "question": "What was the repair team's temporary contact code?",
        },
    }
    (root / "fixture.json").write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["fixture_sha256"] = file_digest(root / "fixture.json")

    def save():
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    for language, value in fixture.items():
        folder = root / language
        folder.mkdir()
        store = SqliteEventStore(folder / "events")
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=folder / "data",
                provider=provider.name,
                model=model,
                context_input=config,
                temperature=0,
                max_steps=12,
                max_output_tokens=2048,
                token_budget=TokenBudgetPolicy("cl100k_base", 32000, 2048, 2048),
                model_retry_policy=ModelRetryPolicy(3, 180, 1, 5, 5, 0),
            ),
            provider=provider,
            event_store=store,
            policies=(HistoryOnly(),),
        )
        case = {"language": language, "question": value["question"], "expected": nonce}
        report["cases"].append(case)
        save()
        try:
            session = None
            for index in range(6):
                fact = value["source"] if index == 4 else f"背景记录 {index}: 已清点普通器材。"
                text = "请记下这条独立记录，只回复已记下：" + fact
                result = (
                    await runtime.run(folder, text)
                    if session is None
                    else await runtime.run_existing(session, text)
                )
                session = result.session_id
                case["session_id"] = session
                print(
                    json.dumps(
                        {"language": language, "setup_turn": index + 1, "reason": result.reason}
                    ),
                    flush=True,
                )
            events = await runtime.sessions.read_session(session)
            await runtime.compaction.replace_through(
                session,
                through_seq=events[-1].seq,
                summary="早先记录了一些工作协调事项，细节在原始历史中。",
            )
            before = len(await runtime.sessions.read_session(session))
            result = await runtime.run_existing(session, value["question"])
            events = await runtime.sessions.read_session(session)
            case.update(
                answer=result.final_text,
                steps=result.steps,
                reason=result.reason,
                events=[e.to_dict() for e in events[before:]],
            )
            case["replay_errors"] = list(
                await verify_request_snapshots(runtime.sessions, runtime.surface, session)
            )
            case["invariant_errors"] = [str(v) for v in await runtime.check_invariants(session)]
            case["evidence_seen"] = any(
                nonce in b["body"]
                for e in events[before:]
                if e.type == "context/input"
                for b in e.data["blocks"]
                if b["tier"] in {"search", "section", "chunk"}
            )
            case["search_used"] = any(
                e.type == "tool/result"
                and e.data.get("tool_name") == "search_history"
                and e.data["status"] == "succeeded"
                for e in events[before:]
            )
            case["passed"] = (
                nonce in result.final_text
                and case["evidence_seen"]
                and not case["replay_errors"]
                and not case["invariant_errors"]
            )
            save()
            print(
                json.dumps(
                    {k: case[k] for k in ("language", "answer", "steps", "search_used", "passed")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as error:
            case["failure"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
            if case.get("session_id"):
                case["all_events"] = [
                    e.to_dict() for e in await runtime.sessions.read_session(case["session_id"])
                ]
            save()
            raise
        finally:
            await runtime.dispose()
            await store.aclose()
    report["complete"] = True
    save()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
