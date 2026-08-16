"""Dependency-free CLI for TraceHarness v0.3."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from dataclasses import asdict
from pathlib import Path

from traceh.api.llm import ModelResponse
from traceh.cli.chat import INTERRUPTED_EXIT_CODE, chat_target, run_chat
from traceh.cli.console import configure_stdio, default_console
from traceh.cli.env_file import EnvFileError, EnvLoadReport, load_env_file
from traceh.cli.errors import CliConfigurationError
from traceh.evaluation.runner import BenchmarkRunner
from traceh.inspector import SessionInspector
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots

_PROVIDERS = ("scripted", "openai-compatible")

__all__ = ["CliConfigurationError", "build_parser", "main"]


def _add_env_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Load configuration and secrets from this file if it exists (default: .env)",
    )


def _add_storage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, default=None)
    _add_env_file_argument(parser)


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    _add_storage_arguments(parser)
    parser.add_argument(
        "--provider",
        choices=_PROVIDERS,
        default=None,
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--verify-command")


def _from_environment(args: argparse.Namespace, attribute: str, variable: str, default=None):
    current = getattr(args, attribute, None)
    if current is not None:
        return current
    return os.environ.get(variable, default)


def _positive_integer(value: object, *, variable: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise CliConfigurationError(f"{variable} must be an integer") from error
    if parsed < 1:
        raise CliConfigurationError(f"{variable} must be at least 1")
    return parsed


def _configure_from_environment(args: argparse.Namespace) -> EnvLoadReport:
    report = load_env_file(getattr(args, "env_file", None))
    if hasattr(args, "data_dir"):
        args.data_dir = Path(_from_environment(args, "data_dir", "TRACEH_DATA_DIR", ".traceh"))
    if not hasattr(args, "provider"):
        return report

    args.provider = _from_environment(args, "provider", "TRACEH_PROVIDER", "scripted")
    if args.provider not in _PROVIDERS:
        raise CliConfigurationError(
            f"TRACEH_PROVIDER must be one of {', '.join(_PROVIDERS)}; got {args.provider!r}"
        )
    args.model = _from_environment(args, "model", "TRACEH_MODEL")
    args.base_url = _from_environment(args, "base_url", "TRACEH_BASE_URL")
    args.api_key_env = _from_environment(
        args,
        "api_key_env",
        "TRACEH_API_KEY_ENV",
        "OPENAI_API_KEY",
    )
    if hasattr(args, "max_steps"):
        raw_max_steps = _from_environment(args, "max_steps", "TRACEH_MAX_STEPS", 20)
        args.max_steps = _positive_integer(raw_max_steps, variable="TRACEH_MAX_STEPS")
    if hasattr(args, "verify_command"):
        args.verify_command = _from_environment(
            args,
            "verify_command",
            "TRACEH_VERIFY_COMMAND",
        )
    return report


def _provider_and_model(args: argparse.Namespace):
    if args.provider == "scripted":
        provider = (
            # An explicit script is a fixture: running past its end is a real
            # error and stays one.
            ScriptedLlmProvider.from_file(args.script)
            if args.script
            # The built-in placeholder has no turn budget to run out of, so it
            # repeats instead of failing the second turn of `traceh chat`.
            else ScriptedLlmProvider(
                (ModelResponse(content="TraceHarness scripted runtime is ready."),),
                repeat_last=True,
            )
        )
        return provider, args.model or "scripted-model"
    if not args.base_url:
        raise CliConfigurationError(
            "openai-compatible requires --base-url or TRACEH_BASE_URL in the environment file"
        )
    if not args.model:
        raise CliConfigurationError(
            "openai-compatible requires --model or TRACEH_MODEL in the environment file"
        )
    provider = OpenAICompatibleProvider(
        args.base_url,
        api_key_env=args.api_key_env,
    )
    return provider, args.model


def _runtime(args: argparse.Namespace):
    provider, model = _provider_and_model(args)
    config = RuntimeConfig(
        data_dir=args.data_dir,
        provider=provider.name,
        model=model,
        max_steps=args.max_steps,
        verification_command=args.verify_command,
    )
    return build_default_runtime(config, provider=provider)


async def _run(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    session_id = await runtime.create_session(args.workspace, metadata={"cli": True})
    print(f"session_id={session_id}")
    try:
        result = await runtime.run_existing(session_id, args.task)
        print(result.final_text)
        print(
            f"reason={result.reason} steps={result.steps} "
            f"tokens={result.usage.total_tokens} verification={result.verification_passed}"
        )
        return 0 if result.reason == "completed" else 2
    finally:
        await runtime.dispose()


async def _chat(args: argparse.Namespace) -> int:
    workspace, session_id = chat_target(args.workspace, args.session_id)
    configure_stdio()
    runtime = _runtime(args)
    return await run_chat(
        runtime,
        default_console(),
        workspace=workspace,
        session_id=session_id,
    )


async def _resume(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    try:
        recovery, result = await runtime.resume(args.session_id, instruction=args.instruction)
        print(json.dumps({"recovery": asdict(recovery)}, ensure_ascii=False, default=str, indent=2))
        print(result.final_text)
        print(f"reason={result.reason} steps={result.steps}")
        return 0
    finally:
        await runtime.dispose()


async def _recover(args: argparse.Namespace) -> int:
    runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir))
    try:
        report = await runtime.recovery.recover(args.session_id)
        print(json.dumps(asdict(report), ensure_ascii=False, default=str, indent=2))
        return 0
    finally:
        await runtime.dispose()


async def _inspect(args: argparse.Namespace) -> int:
    runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir))
    try:
        inspector = SessionInspector(runtime.sessions, runtime.surface)
        print(await inspector.render_text(args.session_id, include_events=not args.summary_only))
        if args.html:
            output = await inspector.render_html(args.session_id, args.html)
            print(f"html={output}")
        return 0
    finally:
        await runtime.dispose()


async def _replay(args: argparse.Namespace) -> int:
    runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir))
    try:
        inspector = SessionInspector(runtime.sessions, runtime.surface)
        print(await inspector.replay_text(args.session_id))
        violations = await verify_request_snapshots(
            runtime.sessions, runtime.surface, args.session_id
        )
        print(f"\nrequest_reconstruction_violations={len(violations)}")
        return 0 if not violations else 3
    finally:
        await runtime.dispose()


async def _sessions(args: argparse.Namespace) -> int:
    runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir))
    try:
        for session_id in await runtime.sessions.list_sessions():
            events = await runtime.sessions.read_session(session_id)
            workspace = await runtime.sessions.workspace_for(session_id)
            print(f"{session_id}\t{workspace}\t{len(events)} events")
        return 0
    finally:
        await runtime.dispose()


async def _compact(args: argparse.Namespace) -> int:
    runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir))
    try:
        summary = args.summary
        if args.summary_file is not None:
            summary = args.summary_file.read_text(encoding="utf-8")
        report = await runtime.compaction.replace_through(
            args.session_id,
            through_seq=args.through_seq,
            summary=summary,
        )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    finally:
        await runtime.dispose()


async def _eval(args: argparse.Namespace) -> int:
    report = await BenchmarkRunner(args.benchmark, args.output).run()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.success_rate == 1.0 else 4


def _doctor(args: argparse.Namespace) -> int:
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    checks = {
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 12),
        "data_dir": str(data_dir),
        "data_dir_writable": os.access(data_dir, os.W_OK),
        "env_file": str(args.env_report.path) if args.env_report.path else None,
        "env_file_loaded": args.env_report.loaded,
        "provider": args.provider,
        "model": args.model,
        "base_url": args.base_url,
        "api_key_env": args.api_key_env,
        "openai_key_present": bool(os.environ.get(args.api_key_env)),
    }
    print(json.dumps(checks, indent=2))
    return 0 if checks["python_supported"] and checks["data_dir_writable"] else 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traceh", description="TraceHarness Py v0.3")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Create a session and run one agent turn")
    run.add_argument("workspace", type=Path)
    run.add_argument("task")
    _add_runtime_arguments(run)
    run.set_defaults(handler=_run)

    chat = sub.add_parser("chat", help="Talk to the agent over several turns in one session")
    chat.add_argument(
        "workspace",
        type=Path,
        nargs="?",
        default=None,
        help="Workspace for a new session; omit it when using --session-id",
    )
    chat.add_argument(
        "--session-id",
        default=None,
        help="Continue an existing session; its workspace comes from the event log",
    )
    _add_runtime_arguments(chat)
    chat.set_defaults(handler=_chat)

    resume = sub.add_parser("resume", help="Recover a session and continue in a new turn")
    resume.add_argument("session_id")
    resume.add_argument(
        "--instruction",
        default=(
            "Continue the previous task. Re-inspect the workspace before repeating any write or "
            "process side effect."
        ),
    )
    _add_runtime_arguments(resume)
    resume.set_defaults(handler=_resume)

    recover = sub.add_parser("recover", help="Close orphaned calls/steps without starting a model")
    recover.add_argument("session_id")
    _add_storage_arguments(recover)
    recover.set_defaults(handler=_recover)

    inspect = sub.add_parser("inspect", help="Inspect state, invariants and event order")
    inspect.add_argument("session_id")
    _add_storage_arguments(inspect)
    inspect.add_argument("--summary-only", action="store_true")
    inspect.add_argument("--html", type=Path)
    inspect.set_defaults(handler=_inspect)

    replay = sub.add_parser("replay", help="Render the model-visible surface")
    replay.add_argument("session_id")
    _add_storage_arguments(replay)
    replay.set_defaults(handler=_replay)

    sessions = sub.add_parser("sessions", help="List persisted sessions")
    _add_storage_arguments(sessions)
    sessions.set_defaults(handler=_sessions)

    compact = sub.add_parser("compact", help="Append a manual Surface replacement")
    compact.add_argument("session_id")
    compact.add_argument("--through-seq", type=int, required=True)
    _add_storage_arguments(compact)
    summary_group = compact.add_mutually_exclusive_group(required=True)
    summary_group.add_argument("--summary")
    summary_group.add_argument("--summary-file", type=Path)
    compact.set_defaults(handler=_compact)

    evaluate = sub.add_parser("eval", help="Run a benchmark directory")
    evaluate.add_argument("benchmark", type=Path)
    evaluate.add_argument("--output", type=Path, default=Path(".traceh/eval"))
    evaluate.set_defaults(handler=_eval)

    doctor = sub.add_parser("doctor", help="Check the local runtime environment")
    _add_storage_arguments(doctor)
    doctor.add_argument("--provider", choices=_PROVIDERS, default=None)
    doctor.add_argument("--model", default=None)
    doctor.add_argument("--base-url", default=None)
    doctor.add_argument("--api-key-env", default=None)
    doctor.set_defaults(handler=_doctor)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.env_report = _configure_from_environment(args)
    except (CliConfigurationError, EnvFileError) as error:
        parser.error(str(error))
    handler = args.handler
    try:
        if asyncio.iscoroutinefunction(handler):
            code = asyncio.run(handler(args))
        else:
            code = handler(args)
    except (CliConfigurationError, EnvFileError) as error:
        # Usage problems are reported as usage problems, never as a traceback.
        parser.error(str(error))
    except KeyboardInterrupt:
        code = INTERRUPTED_EXIT_CODE
    raise SystemExit(code)


if __name__ == "__main__":
    main()
