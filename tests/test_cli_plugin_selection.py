"""CLI wiring for --plugin and TRACEH_PLUGINS.

run, chat and resume must share one selection rule, and selection must be
resolved during argument configuration - before discovery, before any import.
"""

from __future__ import annotations

import os

import pytest

from traceh.cli.main import _configure_from_environment, build_parser
from traceh.plugins.errors import PluginValidationError


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("TRACEH_PLUGINS", raising=False)
    monkeypatch.delenv("TRACEH_PROVIDER", raising=False)
    monkeypatch.delenv("TRACEH_MODEL", raising=False)
    monkeypatch.delenv("TRACEH_DATA_DIR", raising=False)


RUNTIME_COMMANDS = [
    ["run", ".", "task"],
    ["chat", "."],
    ["resume", "some-session-id"],
]


def configure(argv: list[str]):
    args = build_parser().parse_args(argv)
    _configure_from_environment(args)
    return args


@pytest.mark.parametrize("command", RUNTIME_COMMANDS)
def test_no_plugins_enabled_by_default(command: list[str]) -> None:
    assert configure(command).plugins == ()


@pytest.mark.parametrize("command", RUNTIME_COMMANDS)
def test_environment_variable_enables_plugins(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEH_PLUGINS", "a.one,b.two")
    assert configure(command).plugins == ("a.one", "b.two")


@pytest.mark.parametrize("command", RUNTIME_COMMANDS)
def test_cli_flag_enables_plugins(command: list[str]) -> None:
    args = configure([*command, "--plugin", "a.one", "--plugin", "b.two"])
    assert args.plugins == ("a.one", "b.two")


@pytest.mark.parametrize("command", RUNTIME_COMMANDS)
def test_cli_flag_replaces_the_environment(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --plugin occurrence replaces TRACEH_PLUGINS rather than adding to it."""

    monkeypatch.setenv("TRACEH_PLUGINS", "from.env,another.env")
    args = configure([*command, "--plugin", "only.cli"])
    assert args.plugins == ("only.cli",)


@pytest.mark.parametrize("command", RUNTIME_COMMANDS)
def test_all_runtime_commands_resolve_identically(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEH_PLUGINS", "shared.plugin")
    assert configure(command).plugins == ("shared.plugin",)


def test_invalid_cli_plugin_id_fails_configuration() -> None:
    with pytest.raises(PluginValidationError):
        configure(["run", ".", "task", "--plugin", "Bad Id"])


def test_invalid_environment_plugin_id_fails_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACEH_PLUGINS", "ok.one,Bad Id")
    with pytest.raises(PluginValidationError):
        configure(["run", ".", "task"])


def test_duplicate_cli_plugin_ids_fail_configuration() -> None:
    with pytest.raises(PluginValidationError):
        configure(["run", ".", "task", "--plugin", "a.one", "--plugin", "a.one"])


def test_empty_environment_entry_fails_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEH_PLUGINS", "a.one,,b.two")
    with pytest.raises(PluginValidationError):
        configure(["run", ".", "task"])


def test_plugins_subcommands_do_not_take_a_plugin_flag() -> None:
    """`plugins list` is metadata-only; enabling has no meaning there."""

    args = build_parser().parse_args(["plugins", "list"])
    assert not hasattr(args, "plugins")


def test_cli_error_for_bad_plugin_id_is_a_usage_error(capsys) -> None:
    from traceh.cli.main import main

    with pytest.raises(SystemExit) as info:
        main(["run", ".", "task", "--plugin", "Bad Id"])
    assert info.value.code == 2
    error = capsys.readouterr().err
    assert "Traceback" not in error
    assert "invalid plugin id" in error


def test_cli_error_message_does_not_echo_the_rejected_value(capsys) -> None:
    from traceh.cli.main import main

    secret = "sk-proj-FAKE-FIXTURE-NOT-REAL"
    with pytest.raises(SystemExit):
        main(["run", ".", "task", "--plugin", secret])
    error = capsys.readouterr().err
    assert secret not in error
