from __future__ import annotations

import json
import os

import pytest

from traceh.cli.env_file import EnvFileError, load_env_file, parse_env_file
from traceh.cli.main import (
    CliConfigurationError,
    _configure_from_environment,
    _doctor,
    _provider_and_model,
    build_parser,
)
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.session.compaction import CompactionPolicy
from traceh.session.surface_replacement import MAX_SURFACE_SUMMARY_UTF8_BYTES

_TRACEH_VARIABLES = (
    "TRACEH_PROVIDER",
    "TRACEH_BASE_URL",
    "TRACEH_MODEL",
    "TRACEH_API_KEY_ENV",
    "TRACEH_DATA_DIR",
    "TRACEH_MAX_STEPS",
    "TRACEH_VERIFY_COMMAND",
    "TRACEH_PLUGIN_VERIFIER",
    "TRACEH_PLUGINS",
    "TRACEH_AUTO_COMPACT",
    "TRACEH_AUTO_COMPACT_BYTES",
    "TRACEH_AUTO_COMPACT_SUMMARY_BYTES",
    "TRACEH_AUTO_COMPACT_KEEP_TURNS",
)


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())


def _clear_traceh_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TRACEH_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_env_parser_supports_quotes_comments_and_export() -> None:
    values = parse_env_file(
        """
        # comment
        export SERVICE_KEY='secret value'
        ENDPOINT="https://compatible.example/v1"
        MODEL=custom-model # inline comment
        EMPTY=
        """
    )

    assert values == {
        "SERVICE_KEY": "secret value",
        "ENDPOINT": "https://compatible.example/v1",
        "MODEL": "custom-model",
        "EMPTY": "",
    }


def test_env_parser_rejects_invalid_lines() -> None:
    with pytest.raises(EnvFileError, match="expected NAME=VALUE"):
        parse_env_file("not-an-assignment")


def test_env_file_configures_generic_openai_provider(tmp_path, monkeypatch) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.delenv("CUSTOM_SERVICE_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TRACEH_PROVIDER=openai-compatible",
                "TRACEH_BASE_URL=https://compatible.example/v1",
                "TRACEH_MODEL=custom-model",
                "TRACEH_API_KEY_ENV=CUSTOM_SERVICE_KEY",
                "CUSTOM_SERVICE_KEY=test-secret",
            )
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        ["run", str(tmp_path), "test task", "--env-file", str(env_file)]
    )

    report = _configure_from_environment(args)
    provider, model = _provider_and_model(args)

    assert report.loaded
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://compatible.example/v1"
    assert provider._key() == "test-secret"
    assert model == "custom-model"


def test_cli_and_process_environment_override_env_file(tmp_path, monkeypatch) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.setenv("TRACEH_MODEL", "process-model")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TRACEH_PROVIDER=openai-compatible",
                "TRACEH_BASE_URL=https://file.example/v1",
                "TRACEH_MODEL=file-model",
            )
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--env-file",
            str(env_file),
            "--base-url",
            "https://cli.example/v1",
        ]
    )

    load_report = _configure_from_environment(args)

    assert load_report.loaded
    assert args.base_url == "https://cli.example/v1"
    assert args.model == "process-model"


def test_openai_provider_requires_explicit_endpoint_and_model(tmp_path, monkeypatch) -> None:
    _clear_traceh_environment(monkeypatch)
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--provider",
            "openai-compatible",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    _configure_from_environment(args)

    with pytest.raises(CliConfigurationError, match="requires --base-url"):
        _provider_and_model(args)


def test_cli_accepts_an_explicit_plugin_provider_without_inventing_an_adapter(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--plugin",
            "provider.extension",
            "--provider",
            "tenant-provider",
            "--model",
            "tenant-model",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    _configure_from_environment(args)

    provider, model = _provider_and_model(args)

    assert provider is None
    assert model == "tenant-model"
    assert args.plugins == ("provider.extension",)


def test_cli_rejects_plugin_capabilities_without_an_explicit_plugin(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--provider",
            "tenant-provider",
            "--model",
            "tenant-model",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    with pytest.raises(CliConfigurationError, match="requires at least one explicit"):
        _configure_from_environment(args)


def test_cli_selects_one_named_plugin_verifier(tmp_path, monkeypatch) -> None:
    _clear_traceh_environment(monkeypatch)
    args = build_parser().parse_args(
        [
            "chat",
            str(tmp_path),
            "--plugin",
            "verification.extension",
            "--plugin-verifier",
            "workspace-check",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    _configure_from_environment(args)

    assert args.verifier_name == "workspace-check"


def test_cli_rejects_two_verifier_owners(tmp_path, monkeypatch) -> None:
    _clear_traceh_environment(monkeypatch)
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--plugin",
            "verification.extension",
            "--plugin-verifier",
            "workspace-check",
            "--verify-command",
            "python -m unittest",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    with pytest.raises(CliConfigurationError, match="mutually exclusive"):
        _configure_from_environment(args)


def test_explicit_plugin_verifier_overrides_an_environment_command(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.setenv("TRACEH_VERIFY_COMMAND", "lower-priority-command")
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--plugin",
            "verification.extension",
            "--plugin-verifier",
            "workspace-check",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    _configure_from_environment(args)

    assert args.verifier_name == "workspace-check"
    assert args.verify_command is None


def test_explicit_command_verifier_overrides_an_environment_plugin_verifier(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.setenv("TRACEH_PLUGIN_VERIFIER", "lower-priority-verifier")
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--verify-command",
            "python -m unittest",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    _configure_from_environment(args)

    assert args.verify_command == "python -m unittest"
    assert args.verifier_name is None


def test_two_environment_verifier_owners_are_still_rejected(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.setenv("TRACEH_PLUGINS", "verification.extension")
    monkeypatch.setenv("TRACEH_VERIFY_COMMAND", "python -m unittest")
    monkeypatch.setenv("TRACEH_PLUGIN_VERIFIER", "workspace-check")
    args = build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "test task",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    with pytest.raises(CliConfigurationError, match="mutually exclusive"):
        _configure_from_environment(args)


def test_doctor_reports_key_presence_without_printing_secret(tmp_path, monkeypatch, capsys) -> None:
    _clear_traceh_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TRACEH_PROVIDER=openai-compatible",
                "TRACEH_BASE_URL=https://compatible.example/v1",
                "TRACEH_MODEL=custom-model",
                "TRACEH_API_KEY_ENV=CUSTOM_SERVICE_KEY",
                "CUSTOM_SERVICE_KEY=never-print-this-secret",
            )
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "doctor",
            "--env-file",
            str(env_file),
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )
    args.env_report = _configure_from_environment(args)

    assert _doctor(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["openai_key_present"] is True
    assert report["api_key_env"] == "CUSTOM_SERVICE_KEY"
    assert "never-print-this-secret" not in output


def test_env_loader_does_not_override_existing_process_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_KEY", "process-secret")
    env_file = tmp_path / ".env"
    env_file.write_text("SERVICE_KEY=file-secret\n", encoding="utf-8")

    report = load_env_file(env_file)

    assert report.loaded
    assert "SERVICE_KEY" not in report.applied_keys
    assert os.environ["SERVICE_KEY"] == "process-secret"


def _compaction_args(monkeypatch: pytest.MonkeyPatch, tmp_path, *flags: str):
    _clear_traceh_environment(monkeypatch)
    return build_parser().parse_args(
        [
            "run",
            str(tmp_path),
            "task",
            "--env-file",
            str(tmp_path / "absent.env"),
            *flags,
        ]
    )


def test_automatic_compaction_is_off_unless_it_is_configured(
    tmp_path, monkeypatch
) -> None:
    """Absent configuration means off. It never means a guessed threshold."""

    args = _compaction_args(monkeypatch, tmp_path)
    _configure_from_environment(args)
    assert args.compaction is None


def test_automatic_compaction_requires_every_threshold(tmp_path, monkeypatch) -> None:
    args = _compaction_args(monkeypatch, tmp_path, "--auto-compact", "on")
    with pytest.raises(CliConfigurationError) as failure:
        _configure_from_environment(args)
    assert "--auto-compact-bytes" in str(failure.value)

    args = _compaction_args(
        monkeypatch,
        tmp_path,
        "--auto-compact",
        "on",
        "--auto-compact-bytes",
        "4000",
        "--auto-compact-summary-bytes",
        "800",
    )
    with pytest.raises(CliConfigurationError) as partial:
        _configure_from_environment(args)
    assert "--auto-compact-keep-turns" in str(partial.value)


def test_thresholds_without_an_explicit_switch_are_refused(tmp_path, monkeypatch) -> None:
    args = _compaction_args(monkeypatch, tmp_path, "--auto-compact-bytes", "4000")
    with pytest.raises(CliConfigurationError) as failure:
        _configure_from_environment(args)
    assert "--auto-compact on|off" in str(failure.value)


def test_switching_compaction_off_rejects_leftover_thresholds(
    tmp_path, monkeypatch
) -> None:
    args = _compaction_args(
        monkeypatch, tmp_path, "--auto-compact", "off", "--auto-compact-bytes", "4000"
    )
    with pytest.raises(CliConfigurationError):
        _configure_from_environment(args)

    args = _compaction_args(monkeypatch, tmp_path, "--auto-compact", "off")
    _configure_from_environment(args)
    assert args.compaction is None


def test_a_fully_configured_compaction_policy_is_accepted(tmp_path, monkeypatch) -> None:
    args = _compaction_args(
        monkeypatch,
        tmp_path,
        "--auto-compact",
        "on",
        "--auto-compact-bytes",
        "4000",
        "--auto-compact-summary-bytes",
        "800",
        "--auto-compact-keep-turns",
        "0",
    )
    _configure_from_environment(args)
    assert args.compaction == CompactionPolicy(
        enabled=True,
        trigger_utf8_bytes=4000,
        max_summary_utf8_bytes=800,
        keep_recent_turns=0,
    )


def test_compaction_can_be_configured_from_the_environment(
    tmp_path, monkeypatch
) -> None:
    _clear_traceh_environment(monkeypatch)
    monkeypatch.setenv("TRACEH_AUTO_COMPACT", "on")
    monkeypatch.setenv("TRACEH_AUTO_COMPACT_BYTES", "12000")
    monkeypatch.setenv("TRACEH_AUTO_COMPACT_SUMMARY_BYTES", "1500")
    monkeypatch.setenv("TRACEH_AUTO_COMPACT_KEEP_TURNS", "2")
    args = build_parser().parse_args(
        ["run", str(tmp_path), "task", "--env-file", str(tmp_path / "absent.env")]
    )
    _configure_from_environment(args)
    assert args.compaction is not None
    assert args.compaction.trigger_utf8_bytes == 12_000
    assert args.compaction.keep_recent_turns == 2


def test_an_out_of_range_summary_bound_is_refused(tmp_path, monkeypatch) -> None:
    args = _compaction_args(
        monkeypatch,
        tmp_path,
        "--auto-compact",
        "on",
        "--auto-compact-bytes",
        "4000",
        "--auto-compact-summary-bytes",
        str(MAX_SURFACE_SUMMARY_UTF8_BYTES + 1),
        "--auto-compact-keep-turns",
        "1",
    )
    with pytest.raises(CliConfigurationError):
        _configure_from_environment(args)


def test_read_only_commands_do_not_offer_compaction_flags() -> None:
    """`compact` is a manual human decision; it has no automatic policy."""

    args = build_parser().parse_args(
        ["compact", "sid", "--through-seq", "9", "--summary", "s"]
    )
    assert not hasattr(args, "auto_compact")
