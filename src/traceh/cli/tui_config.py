"""Non-secret TUI launch profiles; the CLI remains the assembly owner."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from copy import copy
from pathlib import Path
from urllib.parse import urlsplit

from traceh.cli.env_file import validate_env_var_name
from traceh.cli.errors import CliConfigurationError, SemanticSummaryConfigurationError

# These are launch inputs, never Session state or credential storage.
PROFILE_NAME = ".traceh-tui.json"


class LaunchConfigurationError(ValueError):
    """Host-written, safe-to-display feedback; never embeds input or raw exceptions."""


PATH_FIELDS = frozenset(
    {
        "workspace",
        "data_dir",
        "env_file",
        "context_config",
        "product_config",
        "script",
        "project_workspace",
    }
)
BASE_FIELDS = (
    "workspace",
    "session_id",
    "provider",
    "model",
    "base_url",
    "api_key_env",
    "env_file",
    "data_dir",
    "max_steps",
    "plugins",
    "context_config",
    "product_config",
    "script",
)
COMPACTION_FIELDS = (
    "auto_compact",
    "auto_compact_bytes",
    "auto_compact_summary_bytes",
    "auto_compact_keep_turns",
)
PROJECT_FIELDS = ("default_project_id", "project_actor_id", "project_workspace")
TOKEN_FIELDS = (
    "token_encoding",
    "context_window_tokens",
    "context_output_reserve",
    "context_safety_margin",
    "context_trigger_percent",
)
FIELDS = BASE_FIELDS + COMPACTION_FIELDS + PROJECT_FIELDS + TOKEN_FIELDS + ("auto_compact_method",)


def form_values(args: argparse.Namespace) -> dict[str, str]:
    result = {}
    for name in FIELDS:
        value = getattr(args, name, None)
        result[name] = (
            " ".join(value)
            if name == "plugins" and value is not None
            else ""
            if value is None
            else str(value)
        )
    return result


def validate_values(values: object) -> dict[str, str]:
    if (
        type(values) is not dict
        or not set(BASE_FIELDS) <= set(values) <= set(FIELDS)
        or any(type(value) is not str for value in values.values())
    ):
        raise LaunchConfigurationError("配置文件字段或版本不受支持。")
    if any(any(ord(char) < 32 for char in value) for value in values.values()):
        raise LaunchConfigurationError("配置字段不能包含控制字符。")
    if values["api_key_env"]:
        try:
            validate_env_var_name(values["api_key_env"], setting="API Key 环境变量名")
        except CliConfigurationError:
            raise LaunchConfigurationError(
                "环境变量名格式无效；密钥请填到临时 API Key 密码框。"
            ) from None
    if values["base_url"]:
        try:
            url = urlsplit(values["base_url"])
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise LaunchConfigurationError(
                "Base URL 需要 HTTP(S) 地址，不能携带凭据、查询参数或片段。"
            )
    return {name: values.get(name, "") for name in FIELDS}


def apply_values(args: argparse.Namespace, values: dict[str, str]) -> argparse.Namespace:
    values = validate_values(values)
    result = copy(args)
    for name, value in values.items():
        value = value.strip()
        if name in PATH_FIELDS:
            parsed = Path(value).expanduser().resolve() if value else None
        elif name == "plugins":
            # Empty is an explicit empty selection, not inheritance from the environment.
            parsed = value.split()
        elif name == "auto_compact_method":
            if value not in {"", "extractive", "semantic"}:
                raise LaunchConfigurationError("摘要方式请选择规则摘录或模型摘要。")
            parsed = value or None
        elif name == "auto_compact":
            if value not in {"", "on", "off"}:
                raise LaunchConfigurationError("自动压缩请选择开启、关闭或沿用启动参数。")
            parsed = value or getattr(args, name, None)
        elif name in COMPACTION_FIELDS:
            try:
                parsed = int(value) if value else None
            except ValueError:
                raise LaunchConfigurationError(
                    "压缩阈值、摘要大小、保留轮数必须填写整数。"
                ) from None
        elif name in TOKEN_FIELDS[1:]:
            try:
                parsed = int(value) if value else None
            except ValueError:
                raise LaunchConfigurationError(
                    "Token 窗口、输出预留、安全余量必须是整数。"
                ) from None
        elif name == "max_steps":
            try:
                parsed = int(value) if value else None
            except ValueError:
                raise LaunchConfigurationError("最大步数必须是正整数。") from None
            if parsed is not None and parsed < 1:
                raise LaunchConfigurationError("最大步数必须是正整数。")
        else:
            parsed = value or None
        setattr(result, name, parsed)
    if result.auto_compact == "off":
        for name in COMPACTION_FIELDS[1:]:
            setattr(result, name, None)
    return result


def load_profile(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        type(raw) is not dict
        or set(raw) != {"format", "launch"}
        or type(raw["format"]) is not int
        or raw["format"] != 1
    ):
        raise ValueError("配置文件字段或版本不受支持。")
    values = dict(validate_values(raw["launch"]))
    # A saved profile is portable across working directories, not across machines.
    for name in PATH_FIELDS:
        if values[name]:
            values[name] = str((path.resolve().parent / values[name]).resolve())
    return values


def atomic_json(path: Path, raw: dict) -> None:
    """Replace only after a complete UTF-8 write; failed writes leave the old file."""
    path = path.expanduser().resolve()
    # No automatic directory creation on a mistyped destination.
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(raw, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_profile(path: Path, values: dict[str, str]) -> None:
    validate_values(values)
    if path.exists():
        # Refuse to overwrite arbitrary files (particularly dotenv/Context files).
        load_profile(path)
    normalized = form_values(apply_values(argparse.Namespace(), values))
    atomic_json(path, {"format": 1, "launch": normalized})


def preflight(args: argparse.Namespace) -> str:
    """Synchronous, before Runtime creation: original parsers, no API or Store."""
    from traceh.chat.config import load_context_host_file
    from traceh.cli.chat import chat_target
    from traceh.cli.main import (
        _configure_from_environment,
        _model_retry_policy,
        _provider_and_model,
    )

    # Preview in a private mapping: live Provider workers never observe draft keys.
    environment = dict(getattr(args, "_launch_base_environment", os.environ))
    resolved = copy(args)
    try:
        _configure_from_environment(resolved, environment=environment)
        _model_retry_policy(resolved)
    except SemanticSummaryConfigurationError as error:
        raise LaunchConfigurationError(str(error)) from None
    except (ValueError, CliConfigurationError, OSError):
        raise LaunchConfigurationError(
            "环境文件或 CLI 配置无效；请检查数值、插件 ID 和环境变量名。"
        ) from None
    validate_values(form_values(resolved))
    try:
        workspace, _ = chat_target(resolved.workspace, resolved.session_id)
    except CliConfigurationError:
        raise LaunchConfigurationError(
            "请填写工作区或已有会话 ID；两者必须且只能填写一个。"
        ) from None
    if workspace is not None and not workspace.is_dir():
        raise LaunchConfigurationError("工作区必须是已经存在的目录。")
    try:
        _, model = _provider_and_model(resolved)
    except (ValueError, CliConfigurationError, OSError):
        raise LaunchConfigurationError(
            "模型配置无效；真实模型需填写模型 ID 和 Base URL，脚本路径需有效。"
        ) from None
    if resolved.context_config is not None:
        try:
            load_context_host_file(resolved.context_config)
        except (ValueError, TypeError, OSError):
            raise LaunchConfigurationError(
                "Context 文件校验失败；请在上下文页检查 JSON、预算和项目配置。"
            ) from None
    if resolved.product_config is not None:
        from traceh.product.config import load_product_host_file

        product = load_product_host_file(resolved.product_config)
        profile = product.host_profile.profile
        if profile.provider_id != resolved.provider or profile.model_id != model:
            raise CliConfigurationError("Product 配置与本次模型不一致。")
    from traceh.cli.activity import validate_heartbeat_seconds

    validate_heartbeat_seconds(resolved.heartbeat_seconds, timeline=resolved.timeline)
    has_key = bool(getattr(args, "tui_api_key", None) or environment.get(resolved.api_key_env))
    return "本地配置校验通过；尚未连接模型。" + (
        "未发现密钥；无认证的本地服务可直接使用。"
        if resolved.provider == "openai-compatible" and not has_key
        else ""
    )
