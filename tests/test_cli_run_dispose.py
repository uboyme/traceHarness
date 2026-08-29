"""`traceh run` must dispose the runtime it built, on every path.

``create_session`` used to sit outside the ``try``, so a failure there - an
unreadable workspace, a store error - returned without ever calling
``runtime.dispose()``. With plugins enabled that leaks more than a runtime: the
activated plugins keep their registrations and their owned background tasks.

The tests assert on a real ``dispose()`` call rather than on side effects, so
they fail if the guard is moved back out.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from traceh.cli import main as cli_main
from traceh.cli.main import main
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import EventStore
from traceh.session.sqlite import SqliteEventStore

_TRACEH_VARIABLES = (
    "TRACEH_PROVIDER",
    "TRACEH_BASE_URL",
    "TRACEH_MODEL",
    "TRACEH_API_KEY_ENV",
    "TRACEH_DATA_DIR",
    "TRACEH_MAX_STEPS",
    "TRACEH_VERIFY_COMMAND",
    "TRACEH_PLUGINS",
)


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own configuration out of these tests entirely.

    Clearing ``TRACEH_*`` is not enough on its own. ``--env-file`` defaults to the
    *relative* path ``.env``, so ``main()`` reads whatever ``.env`` sits in the
    current working directory - which, when pytest runs from the repository root,
    is the developer's real one. It would repopulate provider, base URL, model and
    key-variable name, and a test about dispose ordering would quietly place a
    live model call.

    Moving the working directory to ``tmp_path`` removes the file from reach
    rather than relying on any test remembering to pass ``--env-file``, and in
    particular without relying on a fake ``_runtime`` to intercept the call. The
    real file is never read, opened or printed.
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in _TRACEH_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def test_env_file(tmp_path: Path) -> Path:
    """A test-owned env-file path that deliberately does not exist.

    Pointing `--env-file` here makes "no environment file contributed anything"
    checkable as `EnvLoadReport.loaded is False`. An *empty* file would report
    `loaded=True` with no applied keys, which is a weaker and more easily
    misread signal.
    """

    return tmp_path / "absent.env"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "ws"
    directory.mkdir()
    (directory / "hello.txt").write_text("hi", encoding="utf-8")
    return directory


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"content": "all done"}]), encoding="utf-8")
    return path


class DisposeSpy:
    """Wraps a real runtime and records that dispose actually happened."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self.dispose_calls = 0

    def __getattr__(self, name):
        return getattr(self._runtime, name)

    async def dispose(self) -> None:
        self.dispose_calls += 1
        await self._runtime.dispose()


def install_spy(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    *,
    create_session_error: BaseException | None = None,
) -> list[DisposeSpy]:
    """Make `_runtime()` return a spy wrapping a real scripted runtime."""

    created: list[DisposeSpy] = []

    async def fake_runtime(
        args,
        *,
        event_store: EventStore,
        provider_and_model=None,
        additional_tools=(),
    ):
        del args, provider_and_model, additional_tools
        runtime = build_default_runtime(RuntimeConfig(data_dir=data_dir), event_store=event_store)
        if create_session_error is not None:

            async def exploding(*_args, **_kwargs):
                raise create_session_error

            monkeypatch.setattr(runtime, "create_session", exploding)
        wrapper = DisposeSpy(runtime)
        created.append(wrapper)
        return wrapper

    monkeypatch.setattr(cli_main, "_runtime", fake_runtime)
    return created


def configure(argv: list[str], env_file: Path):
    """Parse and configure with a test-owned env file, proving nothing loaded."""

    parser = cli_main.build_parser()
    args = parser.parse_args([*argv, "--env-file", str(env_file)])
    report = cli_main._configure_from_environment(args)
    # Belt and braces with the chdir above: assert as a fact of every run that no
    # environment file contributed anything. If the repository's `.env` ever came
    # back into reach, this fails here rather than by making a network call.
    assert report.loaded is False, "an environment file was loaded into this test"
    assert report.applied_keys == ()
    args.env_report = report
    return args


async def drive_run(argv: list[str], env_file: Path) -> int:
    """Call the `run` handler directly, so its exception is observable."""

    return await cli_main._run(configure(argv, env_file))


def run_cli(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as info:
        main(argv)
    return int(info.value.code or 0)


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------


def test_dispose_runs_when_the_workspace_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, test_env_file: Path, capsys
) -> None:
    spies = install_spy(monkeypatch, tmp_path / "data")
    missing = tmp_path / "does-not-exist"

    with pytest.raises(NotADirectoryError):
        asyncio.run(drive_run(["run", str(missing), "task"], test_env_file))

    capsys.readouterr()
    assert len(spies) == 1
    assert spies[0].dispose_calls == 1, "runtime was never disposed"


def test_dispose_runs_when_the_store_fails_during_create_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, test_env_file: Path, capsys
) -> None:
    """Not only a bad workspace: any create_session failure must be guarded."""

    spies = install_spy(
        monkeypatch,
        tmp_path / "data",
        create_session_error=RuntimeError("event store unavailable"),
    )

    with pytest.raises(RuntimeError, match="event store unavailable"):
        asyncio.run(drive_run(["run", str(workspace), "task"], test_env_file))

    capsys.readouterr()
    assert spies[0].dispose_calls == 1, "runtime was never disposed"


def test_dispose_runs_when_the_reserved_metadata_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, test_env_file: Path, capsys
) -> None:
    spies = install_spy(
        monkeypatch,
        tmp_path / "data",
        create_session_error=ValueError("traceh_plugins is reserved by TraceHarness"),
    )

    with pytest.raises(ValueError, match="reserved"):
        asyncio.run(drive_run(["run", str(workspace), "task"], test_env_file))

    capsys.readouterr()
    assert spies[0].dispose_calls == 1


def test_nothing_is_printed_when_create_session_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, test_env_file: Path, capsys
) -> None:
    """There is no session id to report, so none must be claimed."""

    install_spy(
        monkeypatch,
        tmp_path / "data",
        create_session_error=RuntimeError("event store unavailable"),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(drive_run(["run", str(workspace), "task"], test_env_file))

    assert "session_id=" not in capsys.readouterr().out


def test_dispose_runs_on_a_normal_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, test_env_file: Path, capsys
) -> None:
    spies = install_spy(monkeypatch, tmp_path / "data")

    code = asyncio.run(drive_run(["run", str(workspace), "say hello"], test_env_file))

    capsys.readouterr()
    assert code == 0
    assert spies[0].dispose_calls == 1


def test_dispose_runs_when_the_turn_itself_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, test_env_file: Path, capsys
) -> None:
    """The pre-existing guarantee must survive the change."""

    spies = install_spy(monkeypatch, tmp_path / "data")

    async def exploding(*_args, **_kwargs):
        raise RuntimeError("turn exploded")

    real_runtime_factory = cli_main._runtime

    async def patched(args, **kwargs):
        wrapper = await real_runtime_factory(args, **kwargs)
        monkeypatch.setattr(wrapper._runtime, "run_existing", exploding)
        return wrapper

    monkeypatch.setattr(cli_main, "_runtime", patched)

    with pytest.raises(RuntimeError, match="turn exploded"):
        asyncio.run(drive_run(["run", str(workspace), "task"], test_env_file))

    capsys.readouterr()
    assert spies[0].dispose_calls == 1


# --------------------------------------------------------------------------
# Isolation from the developer's own configuration
#
# These deliberately use the *real* `_runtime` and pass no `--provider` and no
# `--env-file`, so nothing but the working directory stands between them and the
# repository's `.env`. They are the tests that would fail if the isolation were
# removed - the behavioural tests below would merely start making network calls.
# --------------------------------------------------------------------------


def test_the_repository_env_file_is_out_of_reach() -> None:
    assert not Path(".env").exists(), "the repository .env is still in the working directory"


def test_no_env_file_is_loaded_with_the_default_argument() -> None:
    """The default `--env-file` is the relative path `.env`; it must find nothing."""

    parser = cli_main.build_parser()
    args = parser.parse_args(["run", ".", "task"])
    report = cli_main._configure_from_environment(args)

    assert report.loaded is False
    assert report.applied_keys == ()


def test_provider_settings_do_not_come_from_the_repository(tmp_path: Path) -> None:
    """Provider, base URL, model and key-variable must all be built-in defaults."""

    parser = cli_main.build_parser()
    args = parser.parse_args(["run", str(tmp_path), "task"])
    cli_main._configure_from_environment(args)

    assert args.provider == "scripted"
    assert args.base_url is None
    assert args.model is None
    assert args.api_key_env == "OPENAI_API_KEY"


async def test_the_real_runtime_factory_builds_a_scripted_provider(tmp_path: Path) -> None:
    """Without any monkeypatch, `_runtime()` must not build a network provider."""

    from traceh.llm.scripted import ScriptedLlmProvider

    parser = cli_main.build_parser()
    args = parser.parse_args(["run", str(tmp_path), "task", "--data-dir", str(tmp_path / "d")])
    cli_main._configure_from_environment(args)

    store = SqliteEventStore(Path(args.data_dir) / "events")
    runtime = await cli_main._runtime(args, event_store=store)
    try:
        assert runtime.config.provider == "scripted"
        provider = runtime.loop.compositions.llms.require("scripted")
        assert isinstance(provider, ScriptedLlmProvider)
    finally:
        await runtime.dispose()
        await store.aclose()


def test_an_env_file_outside_the_repository_is_still_honoured(tmp_path: Path) -> None:
    """The isolation must not break the feature: a real env file still applies."""

    env_file = tmp_path / "local.env"
    env_file.write_text("TRACEH_MODEL=fixture-model\n", encoding="utf-8")

    parser = cli_main.build_parser()
    args = parser.parse_args(["run", str(tmp_path), "task", "--env-file", str(env_file)])
    report = cli_main._configure_from_environment(args)

    assert report.loaded is True
    assert args.model == "fixture-model"


# --------------------------------------------------------------------------
# Normal behaviour is unchanged
# --------------------------------------------------------------------------


def test_normal_run_output_and_exit_code_are_unchanged(
    workspace: Path, tmp_path: Path, script: Path, test_env_file: Path, capsys
) -> None:
    code = run_cli(
        [
            "run",
            str(workspace),
            "say hello",
            "--data-dir",
            str(tmp_path / "data"),
            "--script",
            str(script),
            "--provider",
            "scripted",
            "--env-file",
            str(test_env_file),
        ]
    )
    output = capsys.readouterr().out
    lines = output.strip().splitlines()

    assert code == 0
    assert lines[0].startswith("session_id=")
    assert "all done" in output
    assert "reason=completed" in lines[-1]
    assert "steps=" in lines[-1]
    assert "tokens=" in lines[-1]
    assert "verification=" in lines[-1]


def test_session_id_is_still_printed_before_the_turn_result(
    workspace: Path, tmp_path: Path, script: Path, test_env_file: Path, capsys
) -> None:
    run_cli(
        [
            "run",
            str(workspace),
            "say hello",
            "--data-dir",
            str(tmp_path / "data"),
            "--script",
            str(script),
            "--provider",
            "scripted",
            "--env-file",
            str(test_env_file),
        ]
    )
    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0].startswith("session_id=")
    assert any("all done" in line for line in lines[1:])


def test_a_non_completed_run_still_exits_with_two(
    workspace: Path, tmp_path: Path, test_env_file: Path, capsys
) -> None:
    """Exit-code semantics must not shift with the guard."""

    script = tmp_path / "loop.json"
    script.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [{"id": f"c{index}", "name": "list_files", "arguments": {}}],
                }
                for index in range(3)
            ]
        ),
        encoding="utf-8",
    )

    code = run_cli(
        [
            "run",
            str(workspace),
            "keep going",
            "--data-dir",
            str(tmp_path / "data"),
            "--script",
            str(script),
            "--provider",
            "scripted",
            "--env-file",
            str(test_env_file),
            "--max-steps",
            "2",
        ]
    )
    output = capsys.readouterr().out

    assert code == 2
    assert "reason=max_steps_exceeded" in output
