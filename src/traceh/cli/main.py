"""Dependency-free CLI for TraceHarness v0.3."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
from dataclasses import asdict
from pathlib import Path

from traceh.api.llm import ModelResponse
from traceh.cli.activity import DEFAULT_HEARTBEAT_SECONDS, validate_heartbeat_seconds
from traceh.cli.chat import (
    INTERRUPTED_EXIT_CODE,
    ResumeEnvironment,
    chat_target,
    run_chat,
)
from traceh.cli.command_line import escape_for_display
from traceh.cli.console import configure_stdio, default_console
from traceh.cli.env_file import (
    EnvFileError,
    EnvLoadReport,
    load_env_file,
    validate_env_var_name,
)
from traceh.cli.errors import CliConfigurationError
from traceh.cli.plugins import doctor_plugins, inspect_plugin, list_plugins
from traceh.evaluation.runner import BenchmarkRunner
from traceh.evolution.artifacts import ArtifactContractError
from traceh.evolution.candidate_comparison import (
    COMPARISON_EXIT_CODE,
    CandidateComparator,
    CandidateComparisonConfig,
    CandidateComparisonConfigurationError,
    CandidateComparisonEvidenceError,
)
from traceh.evolution.candidate_validation import (
    VALIDATION_EXIT_CODE,
    CandidateValidationConfig,
    CandidateValidationConfigurationError,
    CandidateValidator,
)
from traceh.inspector import SessionInspector
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins import PluginError, resolve_enabled_plugins
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    SessionPluginMismatchError,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.version import __version__

_PROVIDERS = ("scripted", "openai-compatible")
_PLUGIN_CAPABILITY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")

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
        default=None,
        help=(
            "Built-in provider name, or an explicitly enabled plugin provider name"
        ),
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--verify-command")
    parser.add_argument(
        "--plugin-verifier",
        dest="verifier_name",
        default=None,
        metavar="NAME",
        help="Select a named verifier contributed by an explicitly enabled plugin",
    )
    parser.add_argument(
        "--plugin",
        dest="plugins",
        action="append",
        default=None,
        metavar="PLUGIN_ID",
        help=(
            "Explicitly enable an installed traceh.plugins entry point; repeat for more "
            "than one. Any --plugin occurrence replaces TRACEH_PLUGINS entirely."
        ),
    )


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
    if hasattr(args, "plugins"):
        # Resolved once, here, so run, chat and resume share one selection rule
        # and an invalid id fails before discovery imports anything.
        args.plugins = resolve_enabled_plugins(
            args.plugins,
            os.environ.get("TRACEH_PLUGINS"),
        )
    if not hasattr(args, "provider"):
        return report

    args.provider = _from_environment(args, "provider", "TRACEH_PROVIDER", "scripted")
    if (
        not isinstance(args.provider, str)
        or not _PLUGIN_CAPABILITY.fullmatch(args.provider)
    ):
        raise CliConfigurationError(
            "TRACEH_PROVIDER / --provider must be a valid capability name"
        )
    if args.provider not in _PROVIDERS and not getattr(args, "plugins", ()):
        raise CliConfigurationError(
            "a plugin provider requires at least one explicit --plugin / TRACEH_PLUGINS id"
        )
    args.model = _from_environment(args, "model", "TRACEH_MODEL")
    args.base_url = _from_environment(args, "base_url", "TRACEH_BASE_URL")
    explicit_verify_command = getattr(args, "verify_command", None) is not None
    explicit_verifier_name = getattr(args, "verifier_name", None) is not None
    if explicit_verify_command and explicit_verifier_name:
        raise CliConfigurationError(
            "--plugin-verifier and --verify-command are mutually exclusive"
        )
    args.api_key_env = _from_environment(
        args,
        "api_key_env",
        "TRACEH_API_KEY_ENV",
        "OPENAI_API_KEY",
    )
    # Validated here, before any runtime or session exists. A name that cannot be
    # looked up will never find a key, and accepting it only to drop it from the
    # resume command meant the next run silently used a different variable. The
    # rule does not depend on the provider: a scripted run ignoring the key does
    # not make an unusable name valid.
    if args.api_key_env is not None:
        args.api_key_env = validate_env_var_name(
            args.api_key_env, setting="--api-key-env / TRACEH_API_KEY_ENV"
        )
    if hasattr(args, "max_steps"):
        raw_max_steps = _from_environment(args, "max_steps", "TRACEH_MAX_STEPS", 20)
        args.max_steps = _positive_integer(raw_max_steps, variable="TRACEH_MAX_STEPS")
    if hasattr(args, "verify_command"):
        # The two verifier selectors are mutually exclusive, but a selector
        # explicitly written on the command line still outranks the other
        # selector's environment/default value.
        args.verify_command = (
            None
            if explicit_verifier_name
            else _from_environment(
                args,
                "verify_command",
                "TRACEH_VERIFY_COMMAND",
            )
        )
    if hasattr(args, "verifier_name"):
        args.verifier_name = (
            None
            if explicit_verify_command
            else _from_environment(
                args,
                "verifier_name",
                "TRACEH_PLUGIN_VERIFIER",
            )
        )
        if args.verifier_name is not None and (
            not isinstance(args.verifier_name, str)
            or not _PLUGIN_CAPABILITY.fullmatch(args.verifier_name)
        ):
            raise CliConfigurationError(
                "TRACEH_PLUGIN_VERIFIER / --plugin-verifier must be a valid capability name"
            )
        if args.verifier_name is not None and not getattr(args, "plugins", ()):
            raise CliConfigurationError(
                "a plugin verifier requires at least one explicit --plugin / TRACEH_PLUGINS id"
            )
        if args.verifier_name is not None and args.verify_command is not None:
            raise CliConfigurationError(
                "--plugin-verifier and --verify-command are mutually exclusive"
            )
    # Whether the *effective* verifier came from this env file, which is not the
    # same as the file containing the key: an explicit --verify-command wins, so
    # the file would not restore what is actually running.
    args.verifier_from_env_file = bool(
        report.loaded
        and not explicit_verify_command
        and getattr(args, "verify_command", None) is not None
        and "TRACEH_VERIFY_COMMAND" in report.applied_keys
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
    if args.provider not in _PROVIDERS:
        if not args.model:
            raise CliConfigurationError(
                "a plugin provider requires --model or TRACEH_MODEL"
            )
        return None, args.model
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


async def _runtime(args: argparse.Namespace):
    provider, model = _provider_and_model(args)
    config = RuntimeConfig(
        data_dir=args.data_dir,
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        verification_command=args.verify_command,
        verifier_name=args.verifier_name,
    )
    return await build_default_runtime_async(
        config,
        provider=provider,
        verifier_name=args.verifier_name,
        enabled_plugins=getattr(args, "plugins", ()),
    )


async def _run(args: argparse.Namespace) -> int:
    runtime = await _runtime(args)
    # Everything after a successful build belongs inside the guard. Creating the
    # session can fail on its own - an unreadable workspace, a store error - and
    # with that call outside the try, such a failure left the runtime (and any
    # activated plugins) never disposed.
    try:
        session_id = await runtime.create_session(args.workspace, metadata={"cli": True})
        print(f"session_id={session_id}")
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
    runtime = await _runtime(args)
    heartbeat_seconds = validate_heartbeat_seconds(
        args.heartbeat_seconds, timeline=args.timeline
    )
    # Everything non-secret that the resume command needs but `RuntimeConfig`
    # does not carry. The env file is only named when one was actually loaded, so
    # the command never points at a file that had no effect.
    report: EnvLoadReport | None = getattr(args, "env_report", None)
    loaded = report is not None and report.loaded
    resume_environment = ResumeEnvironment(
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        env_file=report.path if loaded else None,
        script=args.script,
        # Which variables the env file actually applied, so the block can say
        # "reloaded for you" instead of "supply it again" only when that is true.
        env_file_supplies=frozenset(report.applied_keys) if loaded else frozenset(),
        verifier_from_env_file=bool(getattr(args, "verifier_from_env_file", False)),
    )
    return await run_chat(
        runtime,
        default_console(),
        workspace=workspace,
        session_id=session_id,
        timeline=args.timeline,
        heartbeat_seconds=heartbeat_seconds,
        resume_environment=resume_environment,
    )


async def _resume(args: argparse.Namespace) -> int:
    runtime = await _runtime(args)
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


def _plugins_list(args: argparse.Namespace) -> int:
    return list_plugins(json_output=args.json)


def _plugins_inspect(args: argparse.Namespace) -> int:
    return inspect_plugin(args.plugin_id, json_output=args.json)


async def _plugins_doctor(args: argparse.Namespace) -> int:
    return await doctor_plugins(args.plugin_ids, json_output=args.json)


async def _plugins_validate(args: argparse.Namespace) -> int:
    try:
        report = await CandidateValidator(
            CandidateValidationConfig(
                candidate=args.candidate,
                core_project=args.core_project,
                output=args.output,
                plugin_id=args.plugin_id,
                distribution=args.distribution,
                wheelhouse=args.wheelhouse,
                allow_index=args.allow_index,
                test_requirements=tuple(args.test_requirement),
                command_timeout_seconds=args.command_timeout_seconds,
                core_timeout_seconds=args.core_timeout_seconds,
            )
        ).run()
    except CandidateValidationConfigurationError:
        _print_validation_configuration_failure(args, "candidate-validation-configuration-invalid")
        return VALIDATION_EXIT_CODE
    except (ArtifactContractError, OSError):
        _print_validation_configuration_failure(args, "candidate-validation-io-failed")
        return VALIDATION_EXIT_CODE
    result = {
        "command": "validate",
        "ok": report.ok,
        "report_json": str(args.output.resolve() / "report.json"),
        "report_markdown": str(args.output.resolve() / "report.md"),
        "artifact": report.artifact.filename if report.artifact is not None else None,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("traceh plugins validate")
        print(f"  ok={str(report.ok).lower()}")
        report_path = escape_for_display(str(args.output.resolve() / "report.md"), limit=500)
        print(f"  report={report_path}")
        if report.artifact is not None:
            artifact_path = escape_for_display(
                str(args.output.resolve() / report.artifact.filename),
                limit=500,
            )
            print(f"  artifact={artifact_path}")
    return 0 if report.ok else VALIDATION_EXIT_CODE


async def _plugins_compare(args: argparse.Namespace) -> int:
    try:
        report = await CandidateComparator(
            CandidateComparisonConfig(
                validation_evidence=args.validation_evidence,
                core_project=args.core_project,
                suite=args.suite,
                output=args.output,
                wheelhouse=args.wheelhouse,
                allow_index=args.allow_index,
                test_requirements=tuple(args.test_requirement),
                command_timeout_seconds=args.command_timeout_seconds,
            )
        ).run()
    except (
        ArtifactContractError,
        CandidateComparisonConfigurationError,
        CandidateComparisonEvidenceError,
        OSError,
    ):
        if args.json:
            print(
                json.dumps(
                    {
                        "command": "compare",
                        "ok": False,
                        "code": "candidate-comparison-failed",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print("candidate comparison configuration or evidence is invalid")
        return COMPARISON_EXIT_CODE
    result = {
        "command": "compare",
        "ok": report.ok,
        "classification": report.classification,
        "improvements": list(report.improvements),
        "regressions": list(report.regressions),
        "report_json": str(args.output.resolve() / "report.json"),
        "report_markdown": str(args.output.resolve() / "report.md"),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("traceh plugins compare")
        print(f"  classification={report.classification}")
        print(f"  improvements={len(report.improvements)}")
        print(f"  regressions={len(report.regressions)}")
        report_path = escape_for_display(str(args.output.resolve() / "report.md"), limit=500)
        print(f"  report={report_path}")
    return 0


def _print_validation_configuration_failure(args: argparse.Namespace, code: str) -> None:
    if args.json:
        print(
            json.dumps(
                {"command": "validate", "ok": False, "code": code},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    print("candidate validation configuration is invalid")


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
    parser = argparse.ArgumentParser(
        prog="traceh",
        description=f"TraceHarness Py v{__version__}",
    )
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
    chat.add_argument(
        "--no-timeline",
        dest="timeline",
        action="store_false",
        help="Do not print live step/tool activity while a turn runs",
    )
    chat.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help=(
            "Seconds between 'still working' lines while a model call or tool runs "
            f"(default {DEFAULT_HEARTBEAT_SECONDS:g}; 0 disables them and keeps the timeline)"
        ),
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

    plugins = sub.add_parser(
        "plugins",
        help="Discover and diagnose installed plugins without starting an agent",
    )
    plugin_sub = plugins.add_subparsers(dest="plugin_command", required=True)

    plugin_list = plugin_sub.add_parser("list", help="List metadata without importing plugins")
    plugin_list.add_argument("--json", action="store_true")
    plugin_list.set_defaults(handler=_plugins_list)

    plugin_inspect = plugin_sub.add_parser(
        "inspect",
        help="Inspect one plugin's distribution and entry-point metadata",
    )
    plugin_inspect.add_argument("plugin_id")
    plugin_inspect.add_argument("--json", action="store_true")
    plugin_inspect.set_defaults(handler=_plugins_inspect)

    plugin_doctor = plugin_sub.add_parser(
        "doctor",
        help="Load, validate, set up, health-check and dispose plugins",
    )
    plugin_doctor.add_argument("plugin_ids", nargs="*")
    plugin_doctor.add_argument("--json", action="store_true")
    plugin_doctor.set_defaults(handler=_plugins_doctor)

    plugin_validate = plugin_sub.add_parser(
        "validate",
        help="Build and independently validate one source-only plugin candidate",
    )
    plugin_validate.add_argument("candidate", type=Path)
    plugin_validate.add_argument("--core-project", type=Path, required=True)
    plugin_validate.add_argument("--output", type=Path, required=True)
    plugin_validate.add_argument("--plugin-id")
    plugin_validate.add_argument("--distribution")
    dependency_source = plugin_validate.add_mutually_exclusive_group(required=True)
    dependency_source.add_argument("--wheelhouse", type=Path)
    dependency_source.add_argument(
        "--allow-index",
        action="store_true",
        help="Allow pip to resolve build, runtime and test dependencies from its index",
    )
    plugin_validate.add_argument("--test-requirement", action="append", default=[])
    plugin_validate.add_argument("--command-timeout-seconds", type=float, default=600.0)
    plugin_validate.add_argument("--core-timeout-seconds", type=float, default=1800.0)
    plugin_validate.add_argument("--json", action="store_true")
    plugin_validate.set_defaults(handler=_plugins_validate)

    plugin_compare = plugin_sub.add_parser(
        "compare",
        help="Compare one exact L2 artifact with its disabled baseline",
    )
    plugin_compare.add_argument("validation_evidence", type=Path)
    plugin_compare.add_argument("--core-project", type=Path, required=True)
    plugin_compare.add_argument(
        "--suite",
        type=Path,
        required=True,
        help="Relative path of a fixed task suite inside the trusted core commit",
    )
    plugin_compare.add_argument("--output", type=Path, required=True)
    comparison_source = plugin_compare.add_mutually_exclusive_group(required=True)
    comparison_source.add_argument("--wheelhouse", type=Path)
    comparison_source.add_argument(
        "--allow-index",
        action="store_true",
        help="Allow pip to resolve comparison-environment dependencies from its index",
    )
    plugin_compare.add_argument("--test-requirement", action="append", default=[])
    plugin_compare.add_argument("--command-timeout-seconds", type=float, default=600.0)
    plugin_compare.add_argument("--json", action="store_true")
    plugin_compare.set_defaults(handler=_plugins_compare)

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
    except (CliConfigurationError, EnvFileError, PluginError) as error:
        parser.error(str(error))
    handler = args.handler
    try:
        if asyncio.iscoroutinefunction(handler):
            code = asyncio.run(handler(args))
        else:
            code = handler(args)
    except (
        CliConfigurationError,
        EnvFileError,
        PluginError,
        SessionPluginMismatchError,
    ) as error:
        # Usage and plugin problems are reported as usage problems, never as a
        # traceback. PluginError messages are written by this repository, so no
        # plugin exception text reaches the terminal here.
        parser.error(str(error))
    except KeyboardInterrupt:
        code = INTERRUPTED_EXIT_CODE
    raise SystemExit(code)


if __name__ == "__main__":
    main()
