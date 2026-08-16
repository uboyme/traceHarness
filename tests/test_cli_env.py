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

_TRACEH_VARIABLES = (
    "TRACEH_PROVIDER",
    "TRACEH_BASE_URL",
    "TRACEH_MODEL",
    "TRACEH_API_KEY_ENV",
    "TRACEH_DATA_DIR",
    "TRACEH_MAX_STEPS",
    "TRACEH_VERIFY_COMMAND",
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
