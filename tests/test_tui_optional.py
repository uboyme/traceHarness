"""F4 optional dependency and presentation contracts without Textual installed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import traceh.cli.main as cli_main
import traceh.tui.runner as tui_runner
from traceh.cli.errors import CliConfigurationError
from traceh.cli.main import build_parser
from traceh.tui.presentation import MAX_BLOCK_CHARS, safe_display_block
from traceh.tui.runner import TUI_INSTALL_HINT, require_textual


def test_chat_parser_selects_the_same_command_with_tui_adapter(tmp_path: Path) -> None:
    args = build_parser().parse_args(["chat", str(tmp_path), "--tui"])
    assert args.command == "chat"
    assert args.tui is True
    assert args.workspace == tmp_path


def test_missing_textual_is_a_clear_error_without_line_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(CliConfigurationError, match="optional extra") as missing:
        require_textual()
    assert str(missing.value) == TUI_INSTALL_HINT


async def test_missing_extra_is_checked_before_event_store_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_created = False

    class _ForbiddenStore:
        def __init__(self, _root: Path) -> None:
            nonlocal store_created
            store_created = True

    def unavailable() -> None:
        raise CliConfigurationError(TUI_INSTALL_HINT)

    monkeypatch.setattr(cli_main, "SqliteEventStore", _ForbiddenStore)
    monkeypatch.setattr(tui_runner, "require_textual", unavailable)
    args = build_parser().parse_args(["chat", str(tmp_path), "--tui"])

    with pytest.raises(CliConfigurationError, match="optional extra"):
        await cli_main._chat(args)
    assert not store_created


def test_importing_core_tui_boundary_does_not_import_textual() -> None:
    probe = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; import traceh.tui; "
            "raise SystemExit(1 if 'textual' in sys.modules else 0)",
        ),
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert probe.returncode == 0


def test_core_tui_boundary_and_safe_display_do_not_require_optional_packages() -> None:
    probe = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; "
            "sys.modules['rich'] = None; "
            "sys.modules['rich.cells'] = None; "
            "sys.modules['rich.text'] = None; "
            "sys.modules['textual'] = None; "
            "import traceh.tui; "
            "from traceh.tui.presentation import safe_display_block; "
            "raise SystemExit(0 if safe_display_block('ok') == 'ok' else 1)",
        ),
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert probe.returncode == 0


def test_textual_is_an_optional_bounded_dependency() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert "textual" not in " ".join(project["dependencies"])
    assert project["optional-dependencies"]["tui"] == ["textual>=8.2.8,<9"]


def test_untrusted_display_is_markup_inert_control_safe_and_bounded() -> None:
    source = "[bold]not markup[/bold]\x1b[2J\rline\u202e\x00" + ("界" * 20_000)
    rendered = safe_display_block(source)
    assert "[bold]not markup[/bold]" in rendered
    assert "\x1b" not in rendered
    assert "\r" not in rendered
    assert "\u202e" not in rendered
    assert "\\x1b" in rendered
    assert "\\r" in rendered
    assert "\\u202e" in rendered
    assert "\\0" in rendered
    assert len(rendered) <= MAX_BLOCK_CHARS


def test_multiline_display_has_a_fixed_line_bound() -> None:
    rendered = safe_display_block("\n".join(f"row-{number}" for number in range(500)))
    assert "row-0" in rendered
    assert "row-159" in rendered
    assert "row-160" not in rendered
    assert rendered.endswith("… (more lines omitted)")
