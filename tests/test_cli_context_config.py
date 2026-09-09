"""F5 configuration and resume proof through the public CLI entry point."""

import json
import os

import pytest
from test_cli_chat import FakeConsole
from test_history_runtime import policy

from traceh.cli.main import main


def test_chat_loads_context_file_and_preserves_it_in_resume(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("TRACEH_"):
            monkeypatch.delenv(name)
    config = tmp_path / "explicit context.json"
    config.write_text(
        json.dumps(
            {"format": 1, "context": policy().to_dict(), "skill_policy": None, "project": None}
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    console = FakeConsole(("/context", "/history", "/session", "/exit"))
    monkeypatch.setattr("traceh.cli.main.default_console", lambda: console.console)
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "chat",
                str(tmp_path),
                "--provider",
                "scripted",
                "--data-dir",
                str(tmp_path / "data"),
                "--context-config",
                str(config),
                "--env-file",
                str(env_file),
                "--no-timeline",
            ]
        )
    assert exited.value.code == 0
    assert "context-not-frozen" in console.output
    assert '"history": []' in console.output
    assert "--context-config" in console.output and "explicit context.json" in console.output


def test_invalid_context_file_is_rejected_before_store_creation(tmp_path, monkeypatch, capsys):
    for name in tuple(os.environ):
        if name.startswith("TRACEH_"):
            monkeypatch.delenv(name)
    config = tmp_path / "invalid.json"
    config.write_text('{"format":0}', encoding="utf-8")
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "chat",
                str(tmp_path),
                "--provider",
                "scripted",
                "--data-dir",
                str(data),
                "--context-config",
                str(config),
                "--env-file",
                str(env_file),
            ]
        )
    assert exited.value.code == 2
    assert not data.exists()
    captured = capsys.readouterr()
    assert "context host configuration invalid" in captured.err + captured.out
