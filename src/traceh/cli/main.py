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
from typing import TYPE_CHECKING

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
from traceh.session.compaction import CompactionError, CompactionPolicy
from traceh.session.event_store import EventStore
from traceh.session.sqlite import EventStoreError, SqliteEventStore
from traceh.tools.policy import ToolPolicy
from traceh.version import __version__

_PROVIDERS = ("scripted", "openai-compatible")
if TYPE_CHECKING:
    from traceh.cli.tui_entry import RestartChat
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
    parser.add_argument("--sandbox-config", type=Path,
                        help="Explicit sandbox policy; process tools fail closed when absent")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--denial-warn-after", type=int, default=None,
                        help="Warn after this many identical consecutive denied steps (>=2)")
    parser.add_argument("--denial-stop-after", type=int, default=None,
                        help="Stop repeated denial after this many steps (greater than warning)")
    parser.add_argument("--disable-repeated-denial-check", action="store_true",
                        help="Disable repeated-denial detection; max-steps still applies")
    _add_model_retry_arguments(parser)
    _add_compaction_arguments(parser)
    parser.add_argument(
        "--token-encoding",
        default=None,
        help="Explicit local tiktoken encoding; estimates are not provider usage",
    )
    parser.add_argument("--context-window-tokens", type=int, default=None)
    parser.add_argument("--context-output-reserve", type=int, default=None)
    parser.add_argument("--context-safety-margin", type=int, default=None)
    parser.add_argument("--context-trigger-percent", type=int, default=None)
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


def _add_compaction_arguments(parser: argparse.ArgumentParser) -> None:
    """Automatic Surface compaction is opt-in and fully explicit.

    There is no inferred threshold and no partially configured policy: turning
    it on requires stating the byte trigger, the summary bound and how many
    recent Turns stay untouched, because a guessed value here silently changes
    what the model is allowed to remember.
    """

    parser.add_argument(
        "--auto-compact",
        choices=("on", "off"),
        default=None,
        help=(
            "Enable automatic Surface compaction before each Turn. Requires "
            "--auto-compact-bytes, --auto-compact-summary-bytes and "
            "--auto-compact-keep-turns."
        ),
    )
    parser.add_argument(
        "--auto-compact-bytes",
        type=int,
        default=None,
        metavar="BYTES",
        help=(
            "Compact once model-visible conversation reaches this many canonical "
            "UTF-8 bytes. This is a byte count, not a token count."
        ),
    )
    parser.add_argument("--auto-compact-summary-bytes", type=int, default=None, metavar="BYTES")
    parser.add_argument("--auto-compact-keep-turns", type=int, default=None, metavar="TURNS")
    parser.add_argument("--auto-compact-method", choices=("extractive", "semantic"), default=None)


#: Each automatic-compaction threshold and the environment variable that may
#: supply it. Nothing here has a built-in default.
_COMPACTION_SETTINGS = (
    ("auto_compact_bytes", "TRACEH_AUTO_COMPACT_BYTES", "--auto-compact-bytes"),
    (
        "auto_compact_summary_bytes",
        "TRACEH_AUTO_COMPACT_SUMMARY_BYTES",
        "--auto-compact-summary-bytes",
    ),
    (
        "auto_compact_keep_turns",
        "TRACEH_AUTO_COMPACT_KEEP_TURNS",
        "--auto-compact-keep-turns",
    ),
)


def _compaction_policy(args: argparse.Namespace) -> CompactionPolicy | None:
    mode = _from_environment(args, "auto_compact", "TRACEH_AUTO_COMPACT")
    supplied = {
        attribute: (
            getattr(args, attribute, None)
            if getattr(args, "auto_compact", None) == "off"
            else _from_environment(args, attribute, variable)
        )
        for attribute, variable, _ in _COMPACTION_SETTINGS
    }
    configured = [
        flag for attribute, _, flag in _COMPACTION_SETTINGS if supplied[attribute] is not None
    ]
    if mode is None:
        if configured:
            raise CliConfigurationError(
                "--auto-compact on|off (TRACEH_AUTO_COMPACT) is required when "
                f"{', '.join(configured)} is configured"
            )
        return None
    if mode not in ("on", "off"):
        raise CliConfigurationError("TRACEH_AUTO_COMPACT must be on or off")
    if mode == "off":
        if configured:
            raise CliConfigurationError(
                f"--auto-compact off cannot be combined with {', '.join(configured)}"
            )
        return None
    missing = [flag for attribute, _, flag in _COMPACTION_SETTINGS if supplied[attribute] is None]
    if missing:
        raise CliConfigurationError(f"--auto-compact on requires {', '.join(missing)}")
    try:
        return CompactionPolicy(
            enabled=True,
            trigger_utf8_bytes=_positive_integer(
                supplied["auto_compact_bytes"], variable="TRACEH_AUTO_COMPACT_BYTES"
            ),
            max_summary_utf8_bytes=_positive_integer(
                supplied["auto_compact_summary_bytes"],
                variable="TRACEH_AUTO_COMPACT_SUMMARY_BYTES",
            ),
            keep_recent_turns=_nonnegative_integer(
                supplied["auto_compact_keep_turns"],
                variable="TRACEH_AUTO_COMPACT_KEEP_TURNS",
            ),
        )
    except ValueError as error:
        raise CliConfigurationError(f"invalid compaction policy: {error}") from None


def _from_environment(args: argparse.Namespace, attribute: str, variable: str, default=None):
    current = getattr(args, attribute, None)
    if getattr(args, "_evaluation_options", None) is not None and (
        attribute in {"provider", "model", "base_url", "api_key_env"}
        or attribute.startswith("model_retry_")
    ):
        return current
    if current is not None:
        return current
    return getattr(args, "_configuration_environment", os.environ).get(variable, default)


def _positive_integer(value: object, *, variable: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise CliConfigurationError(f"{variable} must be an integer") from error
    if parsed < 1:
        raise CliConfigurationError(f"{variable} must be at least 1")
    return parsed


def _nonnegative_integer(value: object, *, variable: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise CliConfigurationError(f"{variable} must be an integer") from error
    if parsed < 0:
        raise CliConfigurationError(f"{variable} cannot be negative")
    return parsed


def _nonnegative_float(value: object, *, variable: str) -> float:
    try:
        parsed = float(str(value))
    except ValueError as error:
        raise CliConfigurationError(f"{variable} must be a number") from error
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise CliConfigurationError(f"{variable} must be finite and non-negative")
    return parsed


def _configure_from_environment(args: argparse.Namespace, *, environment=None) -> EnvLoadReport:
    if any(getattr(args, name, None) is not None for name in ("review", "assess", "compare")):
        _validate_eval_review(args)
        return EnvLoadReport(None, False, ())
    if getattr(args, "command", None) == "eval" and args.env_file is None:
        args.env_file = Path(".env")
    if getattr(args, "run_plan", None) is not None:
        from traceh.evaluation.errors import EvaluationError
        from traceh.evaluation.plan import configure_cli_plan
        try:
            configure_cli_plan(args)
        except EvaluationError as error:
            raise CliConfigurationError(error.code) from None
    target = os.environ if environment is None else environment
    args._configuration_environment = dict(target)
    try:
        report = _resolve_environment(args)
        for name in report.applied_keys:
            target[name] = args._configuration_environment[name]
        return report
    finally:
        del args._configuration_environment


def _resolve_environment(args: argparse.Namespace) -> EnvLoadReport:
    environment = args._configuration_environment
    file_environment = (
        dict(environment) if getattr(args, "_personal_env_only", False) else environment
    )
    report = load_env_file(getattr(args, "env_file", None), environment=file_environment)
    if file_environment is not environment:
        key_name = args.api_key_env or file_environment.get("TRACEH_API_KEY_ENV", "OPENAI_API_KEY")
        allowed = {
            "TRACEH_PROVIDER",
            "TRACEH_MODEL",
            "TRACEH_BASE_URL",
            "TRACEH_API_KEY_ENV",
            "TRACEH_MAX_STEPS",
            key_name,
        }
        applied = tuple(name for name in report.applied_keys if name in allowed)
        for name in applied:
            environment[name] = file_environment[name]
        report = EnvLoadReport(report.path, report.loaded, applied)
    if hasattr(args, "data_dir"):
        args.data_dir = Path(_from_environment(args, "data_dir", "TRACEH_DATA_DIR", ".traceh"))
    if hasattr(args, "plugins"):
        # Resolved once, here, so run, chat and resume share one selection rule
        # and an invalid id fails before discovery imports anything.
        args.plugins = resolve_enabled_plugins(
            args.plugins,
            args._configuration_environment.get("TRACEH_PLUGINS"),
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
    if hasattr(args, "auto_compact"):
        args.compaction = _compaction_policy(args)
    if hasattr(args, "token_encoding"):
        from traceh.llm.token_meter import TokenBudgetPolicy

        values = [
            getattr(args, name, None)
            for name in (
                "token_encoding",
                "context_window_tokens",
                "context_output_reserve",
                "context_safety_margin",
            )
        ]
        args.token_budget = None
        if (
            not any(value is not None for value in values)
            and getattr(args, "context_trigger_percent", None) is not None
        ):
            raise CliConfigurationError(
                "请先配置 Token 编码、窗口、输出预留和安全余量，再设置触发比例。"
            )
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise CliConfigurationError("Token 计量需填写编码、模型窗口、输出预留和安全余量。")
            try:
                percent = getattr(args, "context_trigger_percent", None)
                args.token_budget = TokenBudgetPolicy(
                    *values, trigger_percent=percent if percent is not None else 80
                )
            except ValueError:
                raise CliConfigurationError(
                    "Token 预算无效；输出预留加安全余量须小于窗口。"
                ) from None
    if getattr(args, "auto_compact_method", None) == "semantic" and (
        getattr(args, "token_budget", None) is None
        or getattr(args, "compaction", None) is None
        or not args.compaction.enabled
        or getattr(args, "max_steps", 20) < 2
    ):
        from traceh.cli.errors import SemanticSummaryConfigurationError

        raise SemanticSummaryConfigurationError()
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
        api_key=getattr(args, "tui_api_key", None),
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


def _repeated_denial_policy(args: argparse.Namespace):
    from traceh.runtime.repeated_denial import RepeatedDenialPolicy

    if getattr(args, "disable_repeated_denial_check", False):
        if any(getattr(args, name, None) is not None
               for name in ("denial_warn_after", "denial_stop_after")):
            raise ValueError("cannot set denial thresholds while disabling the check")
        return None
    defaults = RepeatedDenialPolicy()
    return RepeatedDenialPolicy(
        warn_after=(defaults.warn_after if getattr(args, "denial_warn_after", None) is None
                    else args.denial_warn_after),
        stop_after=(defaults.stop_after if getattr(args, "denial_stop_after", None) is None
                    else args.denial_stop_after),
    )


async def _runtime(
    args: argparse.Namespace,
    *,
    event_store: EventStore,
    provider_and_model=None,
    additional_tools: tuple[Tool, ...] = (),
    policies: tuple[ToolPolicy, ...] | None = None,
    include_default_tools: bool = True,
    sandbox_cas_root: Path | None = None,
):
    provider, model = (
        _provider_and_model(args) if provider_and_model is None else provider_and_model
    )
    context_settings = getattr(args, "context_settings", None)
    sandbox = None
    if getattr(args, "sandbox_config", None) is not None:
        from traceh.api.sandbox import SandboxConfiguration
        from traceh.sandbox.config import load_sandbox_file

        try:
            settings = await asyncio.to_thread(load_sandbox_file, args.sandbox_config)
            cas_root = sandbox_cas_root
            if cas_root is None:
                cas_root = await asyncio.to_thread(Path(args.data_dir).absolute)
                cas_root /= "artifacts"
            sandbox = SandboxConfiguration(
                settings.policy, cas_root, settings.plugin_grants,
            )
        except ValueError:
            raise CliConfigurationError("sandbox-host-config-invalid") from None
    config = RuntimeConfig(
        data_dir=args.data_dir,
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        repeated_denial_policy=_repeated_denial_policy(args),
        verification_command=args.verify_command,
        verifier_name=args.verifier_name,
        model_retry_policy=_model_retry_policy(args),
        compaction=getattr(args, "compaction", None),
        token_budget=getattr(args, "token_budget", None),
        semantic_summary=getattr(args, "auto_compact_method", None) == "semantic",
        context_input=context_settings.context if context_settings else None,
        skill_policy=context_settings.skill_policy if context_settings else None,
        memory=context_settings.memory if context_settings else None,
        sandbox=sandbox,
    )
    return await build_default_runtime_async(
        config,
        provider=provider,
        event_store=event_store,
        verifier_name=args.verifier_name,
        additional_tools=additional_tools,
        policies=policies,
        include_default_tools=include_default_tools,
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


async def _chat(args: argparse.Namespace) -> int | RestartChat:
    workspace, session_id = chat_target(args.workspace, args.session_id)
    # Every assembly parses the selected host file afresh, including explicit off.
    args.context_settings = None
    if getattr(args, "context_config", None) is not None:
        from traceh.chat.config import load_context_host_file

        try:
            args.context_settings = load_context_host_file(args.context_config)
        except (ValueError, OSError, TypeError):
            raise CliConfigurationError("context host configuration invalid") from None
    tui_runner = None
    if getattr(args, "background_config", None) is not None and not args.tui:
        raise CliConfigurationError("background optimization currently requires --tui")
    if args.tui:
        # The optional dependency is checked before Store/Runtime/Product
        # assembly, so a missing TUI never creates durable state and never
        # silently falls back to the Line adapter.
        from traceh.tui.runner import require_textual, run_tui

        require_textual()
        tui_runner = run_tui
    product_config = None
    product_host = None
    actions = None
    artifact_cas = None
    read_models = None
    product_configuration_errors: tuple[type[BaseException], ...] = ()
    provider_and_model = _provider_and_model(args)
    additional_tools: tuple[Tool, ...] = ()
    runtime_policies: tuple[ToolPolicy, ...] | None = None
    include_default_tools = True
    if args.product_config is not None:
        # Keep the default CLI surface light: none of Product, Workspace, CAS or
        # Promotion is imported merely because another command imported this
        # module.  The explicit flag is the assembly boundary.
        from traceh.artifacts.cas import LocalArtifactCas
        from traceh.artifacts.errors import ArtifactError
        from traceh.budgets.errors import BudgetError
        from traceh.cli.product import (
            LineProductAdapter,
            product_chat_runtime_policies,
            product_chat_runtime_tools,
        )
        from traceh.product.chat import ProductTurnActions
        from traceh.product.config import load_product_host_file
        from traceh.product.errors import ProductError
        from traceh.product.host import (
            build_product_chat_host,
            build_product_read_models,
        )
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
        runtime_policies = product_chat_runtime_policies()
        include_default_tools = False
    store = SqliteEventStore(Path(args.data_dir) / "events")
    runtime = None
    handed_to_chat = False
    result: int | None = None
    primary: BaseException | None = None
    try:
        if product_config is not None:
            assert actions is not None
            artifact_cas = LocalArtifactCas(product_config.cas_root)
            read_models = build_product_read_models(
                store=store,
                host_profile=product_config.host_profile,
                artifact_cas=artifact_cas,
                max_report_chars=product_config.max_report_chars,
            )
            additional_tools = product_chat_runtime_tools(actions, read_models.memory)
        runtime = await _runtime(
            args,
            event_store=store,
            provider_and_model=provider_and_model,
            additional_tools=additional_tools,
            policies=runtime_policies,
            include_default_tools=include_default_tools,
            sandbox_cas_root=product_config.cas_root if product_config is not None else None,
        )
        if product_config is not None:
            provider, _ = provider_and_model
            assert provider is not None
            assert artifact_cas is not None
            assert read_models is not None
            workspace_provider = LocalGitWorkspaceProvider(
                managed_root=product_config.managed_workspace_root,
                sources={
                    product_config.source_id: product_config.source_repository,
                },
            )
            if runtime.config.memory is not None:
                settings = args.context_settings
                if (
                    dict(settings.sources).get(product_config.source_id)
                    != product_config.source_repository.resolve()
                    or settings.managed_root != product_config.managed_workspace_root.resolve()
                ):
                    raise CliConfigurationError(
                        "Product and Context must name the same source and managed root"
                    )
                workspace_provider = runtime.config.memory.source_resolver
            promotion_targets = LocalBareGitPromotionTargets(
                targets={
                    product_config.promotion_target_id: product_config.promotion_target,
                }
            )
            # The requester evidence Tool was frozen against the same durable
            # log before Runtime construction. UI observation and automatic
            # context use the Runtime's connected PublishingEventStore so their
            # feed remains live; both are stateless fresh readers over one log.
            read_models = build_product_read_models(
                store=runtime.sessions.store,
                host_profile=product_config.host_profile,
                artifact_cas=artifact_cas,
                max_report_chars=product_config.max_report_chars,
            )
            product_host = await build_product_chat_host(
                store=runtime.sessions.store,
                sessions=runtime.sessions,
                data_dir=Path(args.data_dir),
                host_profile=product_config.host_profile,
                providers={args.provider: provider},
                workspace_provider=workspace_provider,
                artifact_cas=artifact_cas,
                promotion_targets=promotion_targets,
                capture_limits=product_config.capture_limits,
                approver_id=product_config.approver_id,
                max_report_chars=product_config.max_report_chars,
                actions=actions,
                read_models=read_models,
                model_retry_policy=runtime.config.model_retry_policy,
                event_feed=runtime.events,
                project_scope=runtime.project_scope,
                context_input=runtime.config.context_input,
                memory_config=runtime.config.memory,
                sandbox=runtime.config.sandbox,
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
            context_config=getattr(args, "context_config", None),
            sandbox_config=getattr(args, "sandbox_config", None),
        )
        if tui_runner is not None:
            handed_to_chat = True
            from copy import copy

            from traceh.cli.tui_config import FIELDS

            settings_args = copy(getattr(args, "_tui_launch_inputs", args))
            for name in FIELDS:
                setattr(settings_args, name, getattr(args, name, None))
            # Preserve only the user's process-local input. A key resolved from
            # saved storage is reloaded after environment resolution on restart.
            settings_args.tui_api_key = getattr(
                getattr(args, "_tui_launch_inputs", args), "tui_api_key", None
            )
            settings_args._launch_base_environment = dict(
                getattr(args, "_launch_base_environment", os.environ)
            )
            # The password widget is always blank; the process-only key can be
            # retained on a same-endpoint restart, never serialized in a profile.
            result = await tui_runner(
                runtime,
                workspace=workspace,
                session_id=session_id,
                timeline=args.timeline,
                heartbeat_seconds=heartbeat_seconds,
                product=product_host,
                settings_args=settings_args,
                **({
                    "background_provider": provider_and_model[0],
                    "background_api_key": getattr(args, "tui_api_key", None),
                } if getattr(args, "background_config", None) is not None else {}),
            )
        else:
            line_product = (
                None
                if product_host is None
                else LineProductAdapter(product_host, data_dir=Path(args.data_dir))
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
                product=line_product,
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
        if product_host is not None and not handed_to_chat:
            try:
                await product_host.aclose()
            except BaseException as error:
                cleanup = error
        if runtime is not None and not handed_to_chat:
            try:
                await runtime.dispose()
            except BaseException as error:
                cleanup = combine_failures(cleanup, error, "chat runtime shutdown failed")
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "chat store shutdown failed")
        combined = combine_failures(primary, cleanup, "chat shutdown failed")
        if combined is not None:
            if args.tui and cleanup is not None:
                raise BaseExceptionGroup("TUI store did not close cleanly", [combined])
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


#: A refused compaction is not a crash: the caller gets the stable code and the
#: history is exactly what it was.
COMPACTION_FAILED_EXIT_CODE = 3


async def _compact(args: argparse.Namespace) -> int:
    async with _plain_runtime_scope(args) as runtime:
        summary = args.summary
        if args.summary_file is not None:
            summary = args.summary_file.read_text(encoding="utf-8")
        try:
            report = await runtime.compaction.replace_through(
                args.session_id,
                through_seq=args.through_seq,
                summary=summary,
            )
        except CompactionError as error:
            # Only the stable code: history, summary text and store detail never
            # reach the terminal from a failure path.
            print(f"compaction failed: {error.code}", file=sys.stderr)
            return COMPACTION_FAILED_EXIT_CODE
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0


#: `traceh eval` measured every attempt it ran, and every task stayed coherent.
EVAL_INCOMPLETE_EXIT_CODE = 4


def _validate_eval_review(args):
    from traceh.evaluation.plan import RETRY_FIELDS, _retry_attribute
    fields = ("benchmark", "run_plan", "provider", "model", "base_url", "api_key_env",
              "script", "sandbox_config", "env_file", "repetitions", "max_trials",
              "eval_timeout_seconds") + tuple(_retry_attribute(f) for f in RETRY_FIELDS)
    if (any(getattr(args, name, None) is not None for name in fields)
            or (args.review is not None and args.judgment_file is not None)
            or (args.assess is not None and args.judgment_file is None)
            or (getattr(args, "compare", None) is not None and args.judgment_file is not None)
            or (getattr(args, "compare", None) is None
                and getattr(args, "assessments", None) is not None)):
        raise CliConfigurationError("eval-review-arguments-conflict")


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
    from traceh.api.json_types import fingerprint
    from traceh.evaluation.errors import EvaluationError
    from traceh.evaluation.plan import RunOptions
    from traceh.evaluation.runner import EvaluationRunner

    if getattr(args, "compare", None) is not None:
        _validate_eval_review(args)
        from traceh.evaluation.comparison import compare_experiment
        try:
            result = compare_experiment(args.compare, args.output, assessments=args.assessments)
        except EvaluationError as error:
            raise CliConfigurationError(error.code) from None
        print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                          "report_json": str((args.output / "report.json").resolve())},
                         ensure_ascii=False, indent=2))
        return 4 if result["status"] == "not_comparable" else 0
    if getattr(args, "review", None) is not None or getattr(args, "assess", None) is not None:
        _validate_eval_review(args)
        from traceh.evaluation.review import assess_run, export_review
        try:
            result = (export_review(args.review, args.output) if args.review is not None
                      else assess_run(args.assess, args.judgment_file, args.output))
        except EvaluationError as error:
            raise CliConfigurationError(error.code) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if (args.benchmark is None or getattr(args, "judgment_file", None) is not None
            or getattr(args, "assessments", None) is not None):
        raise CliConfigurationError("eval-benchmark-required")
    provider, model = _provider_and_model(args)
    if provider is None:
        raise CliConfigurationError("eval requires a directly configured built-in provider")
    expected_implementation = getattr(args, "_evaluation_expected_implementation", None)
    if (expected_implementation is not None
            and f"{type(provider).__module__}.{type(provider).__qualname__}"
            != expected_implementation):
        raise CliConfigurationError("evaluation-run-plan-conflict")
    if args.output.exists():
        raise CliConfigurationError("eval --output must be a directory that does not exist yet")
    from traceh.sandbox.config import load_sandbox_file

    try:
        sandbox = (
            await asyncio.to_thread(load_sandbox_file, args.sandbox_config)
            if getattr(args, "sandbox_config", None) is not None else None
        )
    except ValueError:
        raise CliConfigurationError("sandbox-host-config-invalid") from None
    if sandbox is not None and sandbox.plugin_grants:
        raise CliConfigurationError("eval-application-plugin-process-grants-not-supported")
    try:
        runner = EvaluationRunner(
            args.benchmark,
            args.output,
            provider=provider,
            model_id=model,
            retry_policy=_model_retry_policy(args),
            sandbox=sandbox.policy if sandbox is not None else None,
            options=getattr(args, "_evaluation_options", None) or RunOptions(
                repetitions=(1 if getattr(args, "repetitions", None) is None else args.repetitions),
                max_trials=getattr(args, "max_trials", None),
                timeout_seconds=getattr(args, "eval_timeout_seconds", None),
            ),
            provider_binding={"connection_digest": fingerprint({
                "base_url": args.base_url,
                "script": None if args.script is None else args.script.read_text(encoding="utf-8"),
            }), "network_mode": getattr(
                args, "_evaluation_network_mode", "provider-managed-unverified")},
        )
    except EvaluationError as error:
        raise CliConfigurationError(getattr(error, "code", "benchmark-error")) from None
    try:
        report = await runner.run()
    except EvaluationError as error:
        raise CliConfigurationError(error.code) from None
    if runner.options.variants:
        data = report.to_dict()
        print(json.dumps({"command": "eval", "run_id": data["run_id"],
                          "complete": data["complete"], "comparison": data["status"],
                          "report_json": str((args.output / "comparison/report.json").resolve())},
                         ensure_ascii=False, indent=2))
        return 0 if report.complete else EVAL_INCOMPLETE_EXIT_CODE
    result = {
        "command": "eval",
        "benchmark_id": report.benchmark_id,
        "complete": report.complete,
        "run_id": report.run_id,
        "task_type": report.task_type,
        "attempts_run": sum(t.execution.value != "not_started" for t in report.trials),
        "attempts_measured": sum(t.measured for t in report.trials),
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
        "--tui",
        action="store_true",
        help="Use the optional Textual interface (install traceharness-py[tui])",
    )
    chat.add_argument(
        "--configure",
        action="store_true",
        help="Open the TUI launch configuration panel before creating a runtime",
    )
    chat.add_argument(
        "--tui-profile",
        type=Path,
        default=None,
        help="Launch profile for --tui --configure (default: .traceh-tui.json in cwd)",
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
    chat.add_argument("--background-config", type=Path, default=None,
                      help="Explicit bounded background optimization settings (TUI)")
    chat.add_argument(
        "--context-config",
        type=Path,
        default=None,
        help="Explicit Context, Skill and project Memory host configuration",
    )
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
        help="Run a schema-3 ProductTask or retrieval episode evaluation",
    )
    evaluate.add_argument("benchmark", type=Path, nargs="?")
    review_actions = evaluate.add_mutually_exclusive_group()
    review_actions.add_argument("--review", type=Path)
    review_actions.add_argument("--assess", type=Path)
    review_actions.add_argument("--compare", type=Path)
    evaluate.add_argument("--assessments", type=Path,
                          help="Explicit assessment references for offline paired comparison")
    evaluate.add_argument("--judgment-file", type=Path)
    evaluate.add_argument(
        "--run-plan", type=Path, help="Explicit current or baseline/candidate plan")
    evaluate.add_argument("--repetitions", type=int, default=None)
    evaluate.add_argument("--max-trials", type=int, default=None)
    evaluate.add_argument("--eval-timeout-seconds", type=float, default=None)
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
    evaluate.add_argument("--env-file", type=Path, default=None)
    evaluate.add_argument("--provider", default=None)
    evaluate.add_argument("--model", default=None)
    evaluate.add_argument("--script", type=Path)
    evaluate.add_argument("--sandbox-config", type=Path,
                          help="Explicit host sandbox policy for benchmark execution")
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
    tokens = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(tokens or ["chat", "--tui"])
    if args.command == "chat" and args.tui:
        from traceh.cli.tui_entry import run_interactive

        chat_parser = next(
            action.choices["chat"]
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        supplied = {token.split("=", 1)[0] for token in tokens if token.startswith("--")}
        args.tui_explicit = {
            action.dest
            for action in chat_parser._actions
            if supplied.intersection(action.option_strings)
        }
        if args.workspace is not None:
            args.tui_explicit.add("workspace")
        args.handler = run_interactive
    else:
        if getattr(args, "configure", False) or getattr(args, "tui_profile", None) is not None:
            parser.error("--configure / --tui-profile requires --tui")
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
