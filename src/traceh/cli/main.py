"""Dependency-free CLI for TraceHarness v0.3."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from traceh.api.llm import ModelResponse
from traceh.api.tools import Tool
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
from traceh.concurrency import combine_failures
from traceh.evolution.artifacts import ArtifactContractError
from traceh.evolution.candidate_comparison import (
    COMPARISON_EXIT_CODE,
    CandidateComparator,
    CandidateComparisonConfig,
    CandidateComparisonConfigurationError,
    CandidateComparisonEvidenceError,
)
from traceh.evolution.candidate_promotion import (
    PROMOTION_EXIT_CODE,
    CandidatePromoter,
    CandidatePromotionConfig,
    CandidatePromotionConfigurationError,
    CandidatePromotionEvidenceError,
    CandidatePromotionExecutionError,
    CandidatePromotionRollbackError,
    CandidateRollbackConfig,
    CandidateRollbacker,
)
from traceh.evolution.candidate_validation import (
    VALIDATION_EXIT_CODE,
    CandidateValidationConfig,
    CandidateValidationConfigurationError,
    CandidateValidator,
)
from traceh.inspector import SessionInspector
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.llm.retry import ModelRetryPolicy
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins import PluginError, resolve_enabled_plugins
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    SessionPluginMismatchError,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import EventStore
from traceh.session.sqlite import EventStoreError, SqliteEventStore
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
        help=("Built-in provider name, or an explicitly enabled plugin provider name"),
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    _add_model_retry_arguments(parser)
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


def _add_model_retry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-retry-max-attempts", type=int, default=None)
    parser.add_argument("--model-retry-max-elapsed-seconds", type=float, default=None)
    parser.add_argument("--model-retry-base-delay-seconds", type=float, default=None)
    parser.add_argument("--model-retry-max-delay-seconds", type=float, default=None)
    parser.add_argument("--model-retry-after-cap-seconds", type=float, default=None)
    parser.add_argument("--model-retry-jitter-ratio", type=float, default=None)


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


def _nonnegative_float(value: object, *, variable: str) -> float:
    try:
        parsed = float(str(value))
    except ValueError as error:
        raise CliConfigurationError(f"{variable} must be a number") from error
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise CliConfigurationError(f"{variable} must be finite and non-negative")
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
    if not isinstance(args.provider, str) or not _PLUGIN_CAPABILITY.fullmatch(args.provider):
        raise CliConfigurationError("TRACEH_PROVIDER / --provider must be a valid capability name")
    if args.provider not in _PROVIDERS and not getattr(args, "plugins", ()):
        raise CliConfigurationError(
            "a plugin provider requires at least one explicit --plugin / TRACEH_PLUGINS id"
        )
    args.model = _from_environment(args, "model", "TRACEH_MODEL")
    args.base_url = _from_environment(args, "base_url", "TRACEH_BASE_URL")
    explicit_verify_command = getattr(args, "verify_command", None) is not None
    explicit_verifier_name = getattr(args, "verifier_name", None) is not None
    if explicit_verify_command and explicit_verifier_name:
        raise CliConfigurationError("--plugin-verifier and --verify-command are mutually exclusive")
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
    if hasattr(args, "model_retry_max_attempts"):
        args.model_retry_max_attempts = _positive_integer(
            _from_environment(
                args,
                "model_retry_max_attempts",
                "TRACEH_MODEL_RETRY_MAX_ATTEMPTS",
                3,
            ),
            variable="TRACEH_MODEL_RETRY_MAX_ATTEMPTS",
        )
        for attribute, variable, default in (
            ("model_retry_max_elapsed_seconds", "TRACEH_MODEL_RETRY_MAX_ELAPSED_SECONDS", 30.0),
            ("model_retry_base_delay_seconds", "TRACEH_MODEL_RETRY_BASE_DELAY_SECONDS", 0.5),
            ("model_retry_max_delay_seconds", "TRACEH_MODEL_RETRY_MAX_DELAY_SECONDS", 4.0),
            ("model_retry_after_cap_seconds", "TRACEH_MODEL_RETRY_AFTER_CAP_SECONDS", 8.0),
            ("model_retry_jitter_ratio", "TRACEH_MODEL_RETRY_JITTER_RATIO", 0.2),
        ):
            setattr(
                args,
                attribute,
                _nonnegative_float(
                    _from_environment(args, attribute, variable, default),
                    variable=variable,
                ),
            )
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
            raise CliConfigurationError("a plugin provider requires --model or TRACEH_MODEL")
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


def _model_retry_policy(args: argparse.Namespace) -> ModelRetryPolicy:
    try:
        return ModelRetryPolicy(
            max_attempts=args.model_retry_max_attempts,
            max_elapsed_seconds=args.model_retry_max_elapsed_seconds,
            base_delay_seconds=args.model_retry_base_delay_seconds,
            max_delay_seconds=args.model_retry_max_delay_seconds,
            retry_after_cap_seconds=args.model_retry_after_cap_seconds,
            jitter_ratio=args.model_retry_jitter_ratio,
        )
    except (TypeError, ValueError) as error:
        raise CliConfigurationError(f"invalid model retry policy: {error}") from None


async def _runtime(
    args: argparse.Namespace,
    *,
    event_store: EventStore,
    provider_and_model=None,
    additional_tools: tuple[Tool, ...] = (),
):
    provider, model = (
        _provider_and_model(args) if provider_and_model is None else provider_and_model
    )
    config = RuntimeConfig(
        data_dir=args.data_dir,
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        verification_command=args.verify_command,
        verifier_name=args.verifier_name,
        model_retry_policy=_model_retry_policy(args),
    )
    return await build_default_runtime_async(
        config,
        provider=provider,
        event_store=event_store,
        verifier_name=args.verifier_name,
        additional_tools=additional_tools,
        enabled_plugins=getattr(args, "plugins", ()),
    )


@asynccontextmanager
async def _runtime_scope(
    args: argparse.Namespace,
    *,
    provider_and_model=None,
    additional_tools: tuple[Tool, ...] = (),
):
    """Let one CLI command own its SQLite store outside the borrowed Runtime."""

    store = SqliteEventStore(Path(args.data_dir) / "events")
    runtime = None
    primary: BaseException | None = None
    try:
        runtime = await _runtime(
            args,
            event_store=store,
            provider_and_model=provider_and_model,
            additional_tools=additional_tools,
        )
        yield runtime
    except BaseException as error:
        primary = error
    finally:
        cleanup: BaseException | None = None
        if runtime is not None:
            try:
                await runtime.dispose()
            except BaseException as error:
                cleanup = error
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "CLI runtime/store shutdown failed")
        combined = combine_failures(primary, cleanup, "CLI command and shutdown both failed")
        if combined is not None:
            raise combined


@asynccontextmanager
async def _plain_runtime_scope(args: argparse.Namespace):
    """Own SQLite around a no-plugin, default-provider read/control command."""

    store = SqliteEventStore(Path(args.data_dir) / "events")
    runtime = None
    primary: BaseException | None = None
    try:
        runtime = build_default_runtime(RuntimeConfig(data_dir=args.data_dir), event_store=store)
        yield runtime
    except BaseException as error:
        primary = error
    finally:
        cleanup: BaseException | None = None
        if runtime is not None:
            try:
                await runtime.dispose()
            except BaseException as error:
                cleanup = error
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "CLI runtime/store shutdown failed")
        combined = combine_failures(primary, cleanup, "CLI command and shutdown both failed")
        if combined is not None:
            raise combined


async def _run(args: argparse.Namespace) -> int:
    async with _runtime_scope(args) as runtime:
        session_id = await runtime.create_session(args.workspace, metadata={"cli": True})
        print(f"session_id={session_id}")
        result = await runtime.run_existing(session_id, args.task)
        print(result.final_text)
        print(
            f"reason={result.reason} steps={result.steps} "
            f"tokens={result.usage.total_tokens} verification={result.verification_passed}"
        )
        return 0 if result.reason == "completed" else 2


async def _chat(args: argparse.Namespace) -> int:
    workspace, session_id = chat_target(args.workspace, args.session_id)
    product_config = None
    product_host = None
    actions = None
    product_configuration_errors: tuple[type[BaseException], ...] = ()
    provider_and_model = _provider_and_model(args)
    additional_tools: tuple[Tool, ...] = ()
    if args.product_config is not None:
        # Keep the default CLI surface light: none of Product, Workspace, CAS or
        # Promotion is imported merely because another command imported this
        # module.  The explicit flag is the assembly boundary.
        from traceh.artifacts.cas import LocalArtifactCas
        from traceh.artifacts.errors import ArtifactError
        from traceh.budgets.errors import BudgetError
        from traceh.cli.product import LineProductAdapter
        from traceh.product.chat import (
            ConfirmProductTaskTool,
            ProductTurnActions,
            ProposeProductTaskTool,
        )
        from traceh.product.config import load_product_host_file
        from traceh.product.errors import ProductError
        from traceh.product.host import build_product_chat_host
        from traceh.promotion.errors import PromotionError
        from traceh.promotion.local_git import LocalBareGitPromotionTargets
        from traceh.workspaces.errors import WorkspaceError
        from traceh.workspaces.local_git import LocalGitWorkspaceProvider

        product_configuration_errors = (
            ArtifactError,
            BudgetError,
            ProductError,
            PromotionError,
            WorkspaceError,
        )

        try:
            product_config = load_product_host_file(args.product_config)
        except ProductError as error:
            raise CliConfigurationError(error.code) from None
        profile = product_config.host_profile.profile
        provider, model = provider_and_model
        if profile.provider_id != args.provider or profile.model_id != model:
            raise CliConfigurationError(
                "the Product profile provider/model must match this Chat runtime"
            )
        if provider is None:
            raise CliConfigurationError(
                "Product Chat currently requires a directly configured built-in provider"
            )
        actions = ProductTurnActions()
        additional_tools = (
            ProposeProductTaskTool(actions),
            ConfirmProductTaskTool(actions),
        )
    store = SqliteEventStore(Path(args.data_dir) / "events")
    runtime = None
    handed_to_chat = False
    result: int | None = None
    primary: BaseException | None = None
    try:
        runtime = await _runtime(
            args,
            event_store=store,
            provider_and_model=provider_and_model,
            additional_tools=additional_tools,
        )
        if product_config is not None:
            provider, _ = provider_and_model
            assert provider is not None
            workspace_provider = LocalGitWorkspaceProvider(
                managed_root=product_config.managed_workspace_root,
                sources={
                    product_config.source_id: product_config.source_repository,
                },
            )
            promotion_targets = LocalBareGitPromotionTargets(
                targets={
                    product_config.promotion_target_id: product_config.promotion_target,
                }
            )
            product_host = LineProductAdapter(
                await build_product_chat_host(
                    store=runtime.sessions.store,
                    data_dir=Path(args.data_dir),
                    host_profile=product_config.host_profile,
                    providers={args.provider: provider},
                    workspace_provider=workspace_provider,
                    artifact_cas=LocalArtifactCas(product_config.cas_root),
                    promotion_targets=promotion_targets,
                    capture_limits=product_config.capture_limits,
                    approver_id=product_config.approver_id,
                    max_report_chars=product_config.max_report_chars,
                    actions=actions,
                    model_retry_policy=runtime.config.model_retry_policy,
                    event_feed=runtime.events,
                ),
                data_dir=Path(args.data_dir),
            )
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
            product_config=args.product_config,
        )
        handed_to_chat = True
        result = await run_chat(
            runtime,
            default_console(),
            workspace=workspace,
            session_id=session_id,
            timeline=args.timeline,
            heartbeat_seconds=heartbeat_seconds,
            resume_environment=resume_environment,
            product=product_host,
        )
    except BaseException as error:
        if product_configuration_errors and isinstance(error, product_configuration_errors):
            primary = CliConfigurationError(
                getattr(error, "code", "product-host-configuration-invalid")
            )
        else:
            primary = error
    finally:
        cleanup: BaseException | None = None
        if runtime is not None and not handed_to_chat:
            try:
                await runtime.dispose()
            except BaseException as error:
                cleanup = error
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "chat store shutdown failed")
        combined = combine_failures(primary, cleanup, "chat shutdown failed")
        if combined is not None:
            raise combined
    assert result is not None
    return result


async def _resume(args: argparse.Namespace) -> int:
    async with _runtime_scope(args) as runtime:
        recovery, result = await runtime.resume(args.session_id, instruction=args.instruction)
        print(json.dumps({"recovery": asdict(recovery)}, ensure_ascii=False, default=str, indent=2))
        print(result.final_text)
        print(f"reason={result.reason} steps={result.steps}")
        return 0


async def _recover(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
        report = await runtime.recovery.recover(args.session_id)
        print(json.dumps(asdict(report), ensure_ascii=False, default=str, indent=2))
        return 0


async def _inspect(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
        inspector = SessionInspector(runtime.sessions, runtime.surface)
        print(await inspector.render_text(args.session_id, include_events=not args.summary_only))
        if args.html:
            output = await inspector.render_html(args.session_id, args.html)
            print(f"html={output}")
        return 0


async def _replay(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
        inspector = SessionInspector(runtime.sessions, runtime.surface)
        print(await inspector.replay_text(args.session_id))
        violations = await verify_request_snapshots(
            runtime.sessions, runtime.surface, args.session_id
        )
        print(f"\nrequest_reconstruction_violations={len(violations)}")
        return 0 if not violations else 3


async def _sessions(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
        for session_id in await runtime.sessions.list_sessions():
            events = await runtime.sessions.read_session(session_id)
            workspace = await runtime.sessions.workspace_for(session_id)
            print(f"{session_id}\t{workspace}\t{len(events)} events")
        return 0


async def _compact(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
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


#: `traceh eval` measured every attempt it ran, and every task stayed coherent.
EVAL_INCOMPLETE_EXIT_CODE = 4


async def _eval(args: argparse.Namespace) -> int:
    """Run the ProductTask benchmark and write its two consistent reports.

    The exit code answers "did the measurement complete", not "did the tasks
    succeed". A benchmark whose exit code fell over on a failed coding task
    would report a real result as a tool error; a benchmark that could not
    derive a metric it promised is the failure worth signalling.
    """

    # Imported here for the same reason `chat` does it: no other command should
    # pull in Product, Workspace, Artifact and Promotion just by importing this
    # module.
    from traceh.evaluation.errors import EvaluationError
    from traceh.evaluation.runner import ProductBenchmarkRunner

    provider, model = _provider_and_model(args)
    if provider is None:
        raise CliConfigurationError("eval requires a directly configured built-in provider")
    if args.output.exists():
        raise CliConfigurationError("eval --output must be a directory that does not exist yet")
    try:
        runner = ProductBenchmarkRunner(
            args.benchmark,
            args.output,
            provider=provider,
            model_id=model,
            retry_policy=_model_retry_policy(args),
        )
    except EvaluationError as error:
        raise CliConfigurationError(getattr(error, "code", "benchmark-error")) from None
    report = await runner.run()
    result = {
        "command": "eval",
        "benchmark_id": report.benchmark_id,
        "complete": report.complete,
        "attempts_run": len(report.attempts),
        "attempts_measured": report.measured,
        "report_json": str((args.output / "report.json").resolve()),
        "report_markdown": str((args.output / "report.md").resolve()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.complete else EVAL_INCOMPLETE_EXIT_CODE


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


async def _plugins_promote(args: argparse.Namespace) -> int:
    try:
        report = await CandidatePromoter(
            CandidatePromotionConfig(
                validation_evidence=args.validation_evidence,
                comparison_evidence=args.comparison_evidence,
                target_python=args.target_python,
                registry=args.registry,
                output=args.output,
                approval_digest=args.approve,
                command_timeout_seconds=args.command_timeout_seconds,
            )
        ).run()
    except (
        CandidatePromotionConfigurationError,
        CandidatePromotionEvidenceError,
        CandidatePromotionExecutionError,
        CandidatePromotionRollbackError,
        OSError,
    ):
        return _print_promotion_failure(args, "candidate-promotion-failed")
    result = {
        "command": "promote",
        "ok": report.ok,
        "action": report.action,
        "code": report.code,
        "approval_digest": report.approval_digest,
        "promotion_id": report.promotion_id,
        "report_json": str(args.output.resolve() / "report.json"),
        "report_markdown": str(args.output.resolve() / "report.md"),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("traceh plugins promote")
        print(f"  action={report.action}")
        print(f"  code={report.code}")
        print(f"  approval_digest={report.approval_digest}")
        print(f"  promotion_id={report.promotion_id or 'none'}")
        report_path = escape_for_display(str(args.output.resolve() / "report.md"), limit=500)
        print(f"  report={report_path}")
    return 0


async def _plugins_rollback(args: argparse.Namespace) -> int:
    try:
        report = await CandidateRollbacker(
            CandidateRollbackConfig(
                target_python=args.target_python,
                registry=args.registry,
                output=args.output,
                plugin_id=args.plugin_id,
                distribution=args.distribution,
                current_promotion_id=args.current_promotion_id,
                command_timeout_seconds=args.command_timeout_seconds,
            )
        ).run()
    except (
        CandidatePromotionConfigurationError,
        CandidatePromotionEvidenceError,
        CandidatePromotionExecutionError,
        CandidatePromotionRollbackError,
        OSError,
    ):
        return _print_promotion_failure(args, "candidate-rollback-failed")
    result = {
        "command": "rollback",
        "ok": report.ok,
        "code": report.code,
        "promotion_id": report.promotion_id,
        "report_json": str(args.output.resolve() / "report.json"),
        "report_markdown": str(args.output.resolve() / "report.md"),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("traceh plugins rollback")
        print(f"  code={report.code}")
        print(f"  promotion_id={report.promotion_id or 'absent'}")
        report_path = escape_for_display(str(args.output.resolve() / "report.md"), limit=500)
        print(f"  report={report_path}")
    return 0


def _print_promotion_failure(args: argparse.Namespace, code: str) -> int:
    command = getattr(args, "plugin_command", "promote")
    if args.json:
        print(
            json.dumps(
                {"command": command, "ok": False, "code": code},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("candidate promotion or rollback evidence is invalid")
    return PROMOTION_EXIT_CODE


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
    chat.add_argument(
        "--product-config",
        type=Path,
        default=None,
        help=(
            "Enable the controlled ProductTask surface with one explicit schema-1 "
            "host configuration"
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

    evaluate = sub.add_parser(
        "eval",
        help="Run the ProductTask benchmark defined by a benchmark.json manifest",
    )
    evaluate.add_argument("benchmark", type=Path)
    evaluate.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New evidence directory; it must not exist yet",
    )
    # Deliberately narrower than `_add_runtime_arguments`: a benchmark owns its
    # own data directories, verifier and repositories, so `--data-dir`,
    # `--verify-command`, `--plugin-verifier`, `--max-steps` and `--plugin` would
    # be arguments this command cannot honour.
    _add_env_file_argument(evaluate)
    evaluate.add_argument("--provider", default=None)
    evaluate.add_argument("--model", default=None)
    evaluate.add_argument("--script", type=Path)
    evaluate.add_argument("--base-url", default=None)
    evaluate.add_argument("--api-key-env", default=None)
    _add_model_retry_arguments(evaluate)
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

    plugin_promote = plugin_sub.add_parser(
        "promote",
        help="Review or explicitly approve one exact L2/L3 candidate artifact",
    )
    plugin_promote.add_argument("validation_evidence", type=Path)
    plugin_promote.add_argument("comparison_evidence", type=Path)
    plugin_promote.add_argument("--target-python", type=Path, required=True)
    plugin_promote.add_argument("--registry", type=Path, required=True)
    plugin_promote.add_argument("--output", type=Path, required=True)
    plugin_promote.add_argument(
        "--approve",
        metavar="SHA256",
        help="Exact approval digest emitted by a prior review-only invocation",
    )
    plugin_promote.add_argument("--command-timeout-seconds", type=float, default=600.0)
    plugin_promote.add_argument("--json", action="store_true")
    plugin_promote.set_defaults(handler=_plugins_promote)

    plugin_rollback = plugin_sub.add_parser(
        "rollback",
        help="Restore the exact previous managed plugin state",
    )
    plugin_rollback.add_argument("--target-python", type=Path, required=True)
    plugin_rollback.add_argument("--registry", type=Path, required=True)
    plugin_rollback.add_argument("--output", type=Path, required=True)
    plugin_rollback.add_argument("--plugin-id", required=True)
    plugin_rollback.add_argument("--distribution", required=True)
    plugin_rollback.add_argument("--current-promotion-id", required=True)
    plugin_rollback.add_argument("--command-timeout-seconds", type=float, default=600.0)
    plugin_rollback.add_argument("--json", action="store_true")
    plugin_rollback.set_defaults(handler=_plugins_rollback)

    doctor = sub.add_parser("doctor", help="Check the local runtime environment")
    _add_storage_arguments(doctor)
    doctor.add_argument("--provider", choices=_PROVIDERS, default=None)
    doctor.add_argument("--model", default=None)
    doctor.add_argument("--base-url", default=None)
    doctor.add_argument("--api-key-env", default=None)
    doctor.set_defaults(handler=_doctor)
    return parser


def main(argv: list[str] | None = None) -> None:
    # Every command may render persisted model or verifier text, not only Chat.
    # Configure before argparse can print help/errors so a valid Unicode scalar
    # never crashes a Windows console that inherited a legacy code page.
    configure_stdio()
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
        EventStoreError,
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
