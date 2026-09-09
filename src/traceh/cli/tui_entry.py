"""One interactive CLI lifetime, with sequential, fully drained Runtime instances."""

from __future__ import annotations

import argparse
import os
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path

from traceh.cli.tui_config import (
    PROFILE_NAME,
    LaunchConfigurationError,
    apply_values,
    form_values,
    load_profile,
    preflight,
    save_profile,
)

PERSONAL_FIELDS = frozenset(
    {"provider", "model", "base_url", "api_key_env", "env_file", "max_steps"}
)


@dataclass(frozen=True)
class RestartChat:
    """Only a launch intention; returned after the original owners have closed."""

    args: argparse.Namespace = field(repr=False)


def personal_profile_path() -> Path:
    return Path.home() / ".traceh" / "settings.json"


def save_personal_profile(values: dict[str, str]) -> Path:
    path = personal_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_profile(
        path, {key: value if key in PERSONAL_FIELDS else "" for key, value in values.items()}
    )
    return path


def initial_settings(args: argparse.Namespace) -> tuple[argparse.Namespace, str]:
    """Personal connection settings, current project settings, then explicit CLI inputs."""
    explicit = getattr(args, "tui_explicit", set())
    supplied = form_values(args)
    values = {name: "" for name in supplied}
    error = ""
    personal_env_only = False
    personal = personal_profile_path()
    project = getattr(args, "tui_profile", None) or Path.cwd() / PROFILE_NAME
    for path, fields in ((personal, PERSONAL_FIELDS), (project, set(values))):
        if not path.exists():
            continue
        try:
            loaded = load_profile(path)
            if path == project and loaded.get("default_project_id"):
                target = supplied["workspace"] or str(Path.cwd())
                if not loaded["project_workspace"] or (
                    Path(loaded["project_workspace"]).resolve() != Path(target).resolve()
                ):
                    loaded["default_project_id"] = loaded["project_actor_id"] = ""
                    loaded["project_workspace"] = ""
            for name in fields:
                # Blank connection values inherit; project-specific empty fields
                # (notably plugin selection) are still explicit empty selections.
                if loaded[name] or name not in PERSONAL_FIELDS:
                    values[name] = loaded[name]
                    if name == "env_file":
                        personal_env_only = path == personal
        except (ValueError, OSError):
            error = "个人或项目启动配置无法读取。请在配置面板检查或选择新的配置文件。"
    for name in explicit:
        if name in values:
            values[name] = supplied[name]
            if name == "env_file":
                personal_env_only = False
    if "env_file" not in explicit and not values["env_file"]:
        values["env_file"] = supplied["env_file"]
    # Auto-discovery never transports a saved Session or another folder's target.
    # An explicitly named launch profile may still intentionally resume a target.
    if getattr(args, "tui_profile", None) is None:
        values["workspace"] = supplied["workspace"] or str(Path.cwd())
        values["session_id"] = supplied["session_id"]
    if "session_id" in explicit:
        values["workspace"] = supplied["workspace"] if "workspace" in explicit else ""
    elif "workspace" in explicit:
        values["session_id"] = ""
    if not values["workspace"] and not values["session_id"]:
        values["workspace"] = str(Path.cwd())
    try:
        candidate = apply_values(args, values)
    except (ValueError, TypeError):
        candidate = copy(args)
        candidate.workspace = Path.cwd() if args.session_id is None else None
        error = "启动配置字段无效。按 F2 修正配置；原文件未修改。"
    candidate.tui_profile = project
    candidate.interactive_tui = True
    candidate._personal_env_only = personal_env_only
    candidate._entry_workspace = Path(candidate.workspace or Path.cwd()).resolve()
    return candidate, error


async def run_interactive(args: argparse.Namespace) -> int:
    from traceh.cli.env_file import EnvFileError
    from traceh.cli.errors import CliConfigurationError
    from traceh.cli.main import _chat, _configure_from_environment
    from traceh.plugins import PluginError
    from traceh.runtime.agent_runtime import SessionPluginMismatchError
    from traceh.session.protocol import SessionProtocolError
    from traceh.session.sqlite import EventStoreError
    from traceh.tui.runner import require_textual

    require_textual()
    from traceh.cli.credentials import load_key
    from traceh.cli.startup import contains_old_data, start_fresh
    from traceh.tui.onboarding import OldDataApp, QuickSetupApp
    from traceh.tui.settings import ConfigurationApp

    candidate, error = initial_settings(args)
    show_settings = bool(getattr(args, "configure", False)) or bool(error)
    while True:
        candidate._launch_base_environment = dict(os.environ)
        if not show_settings and not error:
            try:
                environment = dict(os.environ)
                preview = copy(candidate)
                _configure_from_environment(preview, environment=environment)
                if candidate.provider is None and "TRACEH_PROVIDER" not in environment:
                    preview.provider = "openai-compatible"
                if candidate.data_dir is None and "TRACEH_DATA_DIR" not in environment:
                    preview.data_dir = candidate._entry_workspace / ".traceh"
                if not getattr(preview, "tui_api_key", None) and not environment.get(
                    preview.api_key_env
                ):
                    preview.tui_api_key = load_key(preview)
                preflight(preview)
            except (ValueError, OSError, CliConfigurationError, EnvFileError):
                # Keep effective inherited model settings visible, but never expose key values.
                from traceh.cli.tui_config import FIELDS

                draft = copy(candidate)
                for name in FIELDS:
                    setattr(draft, name, getattr(preview, name, None))
                draft.tui_api_key = getattr(preview, "tui_api_key", None)
                setup = QuickSetupApp(draft)
                choice = await setup.run_async()
                if choice is None:
                    return 0
                if choice == "advanced":
                    show_settings = True
                else:
                    candidate = choice
                    continue
        if show_settings or error:
            app = ConfigurationApp(
                candidate,
                form_values(candidate),
                candidate.tui_profile,
                initial_error=error,
            )
            candidate = await app.run_async()
            if candidate is None:
                return 0
        before = dict(os.environ)
        resolved = copy(candidate)
        resolved._tui_launch_inputs = copy(candidate)
        report = None
        result = None
        error = ""
        try:
            resolved._launch_base_environment = before
            report = _configure_from_environment(resolved)
            # An unconfigured interactive entry is not a secretly scripted model.
            # The explicit scripted choice remains available for offline use.
            if candidate.provider is None and "TRACEH_PROVIDER" not in os.environ:
                resolved.provider = "openai-compatible"
            # Keep the selected protocol in the editable draft on a missing-model failure.
            candidate.provider = resolved.provider
            resolved.env_report = report
            if candidate.data_dir is None and "TRACEH_DATA_DIR" not in os.environ:
                resolved.data_dir = candidate._entry_workspace / ".traceh"
            if not getattr(resolved, "tui_api_key", None) and not os.environ.get(
                resolved.api_key_env
            ):
                resolved.tui_api_key = load_key(resolved)
            preflight(resolved)
            if contains_old_data(resolved.data_dir, resolved.session_id):
                choice = await OldDataApp().run_async()
                if choice is None:
                    return 0
                if choice == "fresh":
                    candidate = start_fresh(resolved)
                    show_settings = False
                else:
                    show_settings = True
                continue
            result = await _chat(resolved)
        except (
            CliConfigurationError,
            LaunchConfigurationError,
            EnvFileError,
            PluginError,
            SessionPluginMismatchError,
            EventStoreError,
            SessionProtocolError,
            OSError,
        ):
            error = "运行配置或会话未就绪。请检查模型、路径与会话；已有记录保留。"
        finally:
            # _chat/runner finish their cleanup before this point. Restore only
            # dotenv-owned variables, never mutate a live Provider's environment.
            if report is not None:
                for key in report.applied_keys:
                    if key in before:
                        os.environ[key] = before[key]
                    else:
                        os.environ.pop(key, None)
        show_settings = False
        if isinstance(result, RestartChat):
            candidate = result.args
            continue
        if error:
            continue
        return result
