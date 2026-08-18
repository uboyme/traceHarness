"""The read-only CLI commands still build a plain runtime and still work.

These commands never activate plugins - inspecting or replaying a session must
not import third-party code - so they use the synchronous builder. That is
exactly why they need covering: nothing else in the suite drives them through
``main()``, and a dropped import in this path stayed invisible until a linter
found it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from traceh.cli.main import build_parser, main
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI configuration from leaking into the rest of the suite.

    `main()` loads the current directory's `.env` into `os.environ`. Without this
    the developer's own file would follow these tests into every later test in
    the session - which is exactly how a resume-command test started asserting
    against a model name this file never mentions.
    """

    monkeypatch.setattr(os, "environ", os.environ.copy())


def run_cli(argv: list[str]) -> int:
    """`main()` exits rather than returning; surface the code for assertions."""

    with pytest.raises(SystemExit) as info:
        main(argv)
    return int(info.value.code or 0)


@pytest.fixture
def session(tmp_path: Path) -> tuple[Path, str, int]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")
    data_dir = tmp_path / "data"

    async def create() -> tuple[str, int]:
        runtime = build_default_runtime(RuntimeConfig(data_dir=data_dir))
        try:
            result = await runtime.run(workspace, "say hello")
            assert result.reason == "completed"
            session_id = (await runtime.sessions.list_sessions())[0]
            events = await runtime.sessions.read_session(session_id)
            return session_id, events[-1].seq
        finally:
            await runtime.dispose()

    session_id, last_seq = asyncio.run(create())
    return data_dir, session_id, last_seq


def test_sessions_lists_the_session(session, capsys) -> None:
    data_dir, session_id, _ = session
    assert run_cli(["sessions", "--data-dir", str(data_dir)]) == 0
    assert session_id in capsys.readouterr().out


def test_inspect_reports_no_violations(session, capsys) -> None:
    data_dir, session_id, _ = session
    assert run_cli(["inspect", session_id, "--data-dir", str(data_dir)]) == 0
    output = capsys.readouterr().out
    assert session_id in output
    assert "Invariant violations: 0" in output
    assert "Request reconstruction violations: 0" in output


def test_inspect_can_write_html(session, tmp_path: Path, capsys) -> None:
    data_dir, session_id, _ = session
    target = tmp_path / "report.html"
    code = run_cli(
        ["inspect", session_id, "--data-dir", str(data_dir), "--html", str(target)]
    )
    assert code == 0
    assert f"html={target}" in capsys.readouterr().out
    assert target.exists() and target.read_text(encoding="utf-8").strip()


def test_replay_reports_no_reconstruction_violations(session, capsys) -> None:
    data_dir, session_id, _ = session
    assert run_cli(["replay", session_id, "--data-dir", str(data_dir)]) == 0
    output = capsys.readouterr().out
    assert "request_reconstruction_violations=0" in output


def test_recover_on_a_healthy_session_changes_nothing(session, capsys) -> None:
    data_dir, session_id, _ = session
    assert run_cli(["recover", session_id, "--data-dir", str(data_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False


def test_compact_appends_a_surface_replacement(session, capsys) -> None:
    data_dir, session_id, last_seq = session
    code = run_cli(
        [
            "compact", session_id,
            "--data-dir", str(data_dir),
            "--through-seq", str(last_seq),
            "--summary", "Earlier turn summarised.",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == session_id
    assert payload["source_seqs"], "compaction must record which messages it replaced"
    assert payload["replacement_seq"] > last_seq


def test_read_only_commands_do_not_accept_plugin_selection() -> None:
    """They build a plain runtime, so offering --plugin would be a lie."""

    for argv in (["sessions"], ["inspect", "sid"], ["replay", "sid"], ["recover", "sid"]):
        args = build_parser().parse_args(argv)
        assert not hasattr(args, "plugins")
