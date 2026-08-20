"""Safety and completeness of the printed resume command.

This block exists to be copied into a shell, which makes it the one place where
values from a `.env`, a workspace path or a model name become something a shell
parses. Two properties are load-bearing: nothing derived can escape its quoting,
and nothing whose safety cannot be shown is echoed at all.

Every credential-shaped string below is a deliberately marked test fixture -
`FAKE`/`FIXTURE` and `.invalid` hosts - never a real secret.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from traceh.api.llm import ModelResponse
from traceh.cli.chat import ChatSession, ResumeEnvironment, _safe_base_url, _write_resume_block
from traceh.cli.command_line import (
    Literal,
    UnsafeCommandValue,
    default_shell,
    quote_powershell,
    render_command,
    render_posix,
    render_powershell,
)
from traceh.cli.console import Console
from traceh.cli.main import _configure_from_environment, build_parser
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


class StubOpenAiCompatible:
    """Named provider stub; the runtime validates the name against its config."""

    name = "openai-compatible"

    async def complete(self, request):  # pragma: no cover - never invoked here
        raise AssertionError("the resume block must not call a provider")


class FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    @property
    def console(self) -> Console:
        return Console(read_line=lambda prompt: "", write=self.write)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)

    def command_line(self) -> str:
        return next(line for line in self.lines if "traceh chat" in line).strip()


def build_runtime(
    tmp_path: Path,
    *,
    data_dir: Path | None = None,
    provider: str = "scripted",
    model: str = "qwen-plus",
    verification_command: str | None = None,
    max_steps: int = 20,
):
    return build_default_runtime(
        RuntimeConfig(
            data_dir=data_dir or (tmp_path / ".traceh"),
            provider=provider,
            model=model,
            max_steps=max_steps,
            verification_command=verification_command,
        ),
        provider=(
            StubOpenAiCompatible()
            if provider == "openai-compatible"
            else ScriptedLlmProvider((ModelResponse(content="hi"),), repeat_last=True)
        ),
        event_store=InMemoryEventStore(),
    )


def resume_lines(
    runtime,
    session_id="s1",
    *,
    environment=None,
    shell="powershell",
    workspace=None,
    plugin_ids=None,
):
    console = FakeConsole()
    _write_resume_block(
        runtime,
        console.console,
        ChatSession(session_id, workspace or Path(".")),
        environment or ResumeEnvironment(),
        shell=shell,
        plugin_ids=(runtime.enabled_plugin_ids if plugin_ids is None else plugin_ids),
    )
    return console


# -- shell quoting -------------------------------------------------------

#: Values a shell would otherwise interpret. Each must survive as one token.
HOSTILE_VALUES = [
    "plain",
    "with space",
    "amp&ersand",
    "semi;colon",
    "pipe|value",
    "sub$(whoami)",
    "dollar$var",
    "back`tick",
    "single'quote",
    'double"quote',
    "paren(then)",
    "brace{then}",
    "at@sign",
    "中文 路径/目录",
    "trailing ",
    "&&multiple;;chars||",
]


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_powershell_quoting_produces_one_literal_token(value: str) -> None:
    """A single-quoted PowerShell string is literal; only `'` needs escaping."""

    quoted = quote_powershell(value)

    assert quoted.startswith("'") and quoted.endswith("'")
    # Undo PowerShell's own rule and the original value must come back exactly.
    assert quoted[1:-1].replace("''", "'") == value
    # Every internal quote is doubled, so the string cannot terminate early.
    assert quoted[1:-1].replace("''", "") .count("'") == 0


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_posix_quoting_round_trips_through_the_real_parser(value: str) -> None:
    """POSIX rendering is checked with `shlex`, the parser it targets."""

    rendered = render_posix([Literal("traceh"), Literal("--flag"), value])

    assert shlex.split(rendered) == ["traceh", "--flag", value]


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_a_hostile_value_cannot_add_a_powershell_token(value: str) -> None:
    """The metacharacters must all end up inside one quoted string."""

    rendered = render_powershell([Literal("traceh"), Literal("--flag"), value])

    prefix = "traceh --flag "
    assert rendered.startswith(prefix)
    remainder = rendered[len(prefix):]
    assert remainder == quote_powershell(value)
    # No unquoted separator survived outside the literal string.
    assert remainder.startswith("'") and remainder.endswith("'")


@pytest.mark.parametrize(
    "value", ["line\nbreak", "carriage\rreturn", "nul\x00byte", "esc\x1b[2J", "bidi\u202eflip"]
)
def test_control_characters_are_refused_rather_than_escaped(value: str) -> None:
    """A newline would create a second command line; no quoting rule is trusted."""

    for renderer in (render_powershell, render_posix):
        with pytest.raises(UnsafeCommandValue):
            renderer([Literal("traceh"), value])


def test_a_literal_command_name_is_not_quoted() -> None:
    """PowerShell parses a quoted leading string as an expression, not a command.

    `'traceh' 'chat'` prints the word instead of running anything, so a quoted
    command name yields a command that silently does nothing.
    """

    rendered = render_powershell([Literal("traceh"), Literal("chat"), "value"])

    assert rendered.startswith("traceh chat ")
    assert not rendered.startswith("'")


def test_a_literal_marker_on_an_unsafe_value_still_quotes() -> None:
    """A mistaken marker must degrade to quoting, never to injection."""

    rendered = render_powershell([Literal("traceh"), Literal("not;safe")])

    assert rendered == "traceh 'not;safe'"


def test_each_shell_uses_its_own_rule() -> None:
    """One quoting rule cannot serve both parsers."""

    value = "it's"
    assert quote_powershell(value) == "'it''s'"
    assert shlex.quote(value) == '\'it\'"\'"\'s\''
    assert render_powershell([value]) != render_posix([value])


def test_the_default_shell_follows_the_platform() -> None:
    assert default_shell("win32") == "powershell"
    assert default_shell("linux") == "posix"
    assert default_shell("darwin") == "posix"


def test_an_unknown_shell_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown shell"):
        render_command(["x"], shell="fish")


# -- what must never be echoed ------------------------------------------


def test_a_verifier_command_is_never_echoed(tmp_path: Path) -> None:
    """Arbitrary shell text cannot be shown and also promised secret-free."""

    runtime = build_runtime(
        tmp_path,
        # NOT a real token: a deliberately token-shaped fixture.
        verification_command="pytest --token sk-proj-FAKE0000FIXTURE -k secret",
    )
    console = resume_lines(runtime)

    # Note: "pytest" itself is not asserted absent - tmp_path contains it.
    for fragment in ("sk-proj", "FAKE0000FIXTURE", "--token", "-k secret"):
        assert fragment not in console.output
    assert console.has(
        "Verifier command omitted from the displayed resume command; re-supply it manually."
    )


def _resolved_args(tmp_path: Path, argv: list[str]):
    """Parse and resolve exactly as the CLI does, so provenance is real."""

    args = build_parser().parse_args(argv)
    args.env_report = _configure_from_environment(args)
    return args


def _environment_from(args) -> ResumeEnvironment:
    loaded = args.env_report.loaded
    return ResumeEnvironment(
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        env_file=args.env_report.path if loaded else None,
        script=args.script,
        env_file_supplies=frozenset(args.env_report.applied_keys) if loaded else frozenset(),
        verifier_from_env_file=bool(getattr(args, "verifier_from_env_file", False)),
    )


def test_an_overridden_verifier_is_not_claimed_to_come_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env file holding the key proves nothing about which value won.

    With `TRACEH_VERIFY_COMMAND` in the file *and* an explicit
    `--verify-command`, the explicit one is what runs - so telling the user the
    file will restore it would restore a different verifier.
    """

    monkeypatch.setattr(
        os, "environ", {k: v for k, v in os.environ.items() if not k.startswith("TRACEH_")}
    )
    env_file = tmp_path / ".env"
    env_file.write_text("TRACEH_VERIFY_COMMAND=pytest\n", encoding="utf-8")
    args = _resolved_args(
        tmp_path,
        [
            "chat", str(tmp_path),
            "--env-file", str(env_file),
            # NOT a real token: a deliberately token-shaped fixture.
            "--verify-command", "pytest -q --token sk-proj-FAKE0000FIXTURE",
        ],
    )

    assert args.verify_command == "pytest -q --token sk-proj-FAKE0000FIXTURE"
    assert "TRACEH_VERIFY_COMMAND" in args.env_report.applied_keys
    assert args.verifier_from_env_file is False, "the explicit value won, not the file"

    runtime = build_runtime(tmp_path, verification_command=args.verify_command)
    console = resume_lines(runtime, environment=_environment_from(args))

    assert console.has("re-supply it manually")
    assert not console.has("restored by the env file")
    # ``-q`` alone is not a credential-bearing verifier identity: it can also
    # occur inside an unrelated temporary directory name.  The distinctive
    # token-shaped and option fragments prove the verifier was withheld
    # without making the assertion depend on random parent paths.
    for fragment in ("sk-proj", "FAKE0000FIXTURE", "--token"):
        assert fragment not in console.output


def test_a_verifier_actually_supplied_by_the_env_file_is_reported_as_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit override, so the file really is what restores it."""

    monkeypatch.setattr(
        os, "environ", {k: v for k, v in os.environ.items() if not k.startswith("TRACEH_")}
    )
    env_file = tmp_path / ".env"
    # NOT a real token: a deliberately token-shaped fixture.
    env_file.write_text(
        "TRACEH_VERIFY_COMMAND=pytest --token sk-proj-FAKE0000FIXTURE\n", encoding="utf-8"
    )
    args = _resolved_args(tmp_path, ["chat", str(tmp_path), "--env-file", str(env_file)])

    assert args.verify_command == "pytest --token sk-proj-FAKE0000FIXTURE"
    assert args.verifier_from_env_file is True

    runtime = build_runtime(tmp_path, verification_command=args.verify_command)
    console = resume_lines(runtime, environment=_environment_from(args))

    assert console.has("restored by the env file above")
    assert not console.has("re-supply it manually")
    # Still not echoed, even though the file can restore it.
    for fragment in ("sk-proj", "FAKE0000FIXTURE", "--token"):
        assert fragment not in console.output


def test_a_verifier_from_the_process_environment_is_not_claimed_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing variable wins over the file, which therefore cannot restore it."""

    monkeypatch.setattr(
        os,
        "environ",
        {
            **{k: v for k, v in os.environ.items() if not k.startswith("TRACEH_")},
            "TRACEH_VERIFY_COMMAND": "pytest -x",
        },
    )
    env_file = tmp_path / ".env"
    env_file.write_text("TRACEH_VERIFY_COMMAND=pytest\n", encoding="utf-8")
    args = _resolved_args(tmp_path, ["chat", str(tmp_path), "--env-file", str(env_file)])

    # `.env` never overwrites an existing variable, so it applied nothing here.
    assert args.verify_command == "pytest -x"
    assert args.verifier_from_env_file is False

    runtime = build_runtime(tmp_path, verification_command=args.verify_command)
    console = resume_lines(runtime, environment=_environment_from(args))
    assert console.has("re-supply it manually")
    assert "-x" not in console.output


def test_the_verifier_text_cannot_enter_the_resume_environment() -> None:
    """Provenance travels as a boolean, so the command text has nowhere to sit.

    Keeping it out by construction is what keeps it out of the object's repr and
    therefore out of any log line that ever prints one.
    """

    fields = ResumeEnvironment.__dataclass_fields__
    assert "verifier_from_env_file" in fields
    assert fields["verifier_from_env_file"].type in ("bool", bool)
    for absent in ("verify_command", "verification_command", "verifier"):
        assert absent not in fields

    assert "pytest" not in repr(ResumeEnvironment(verifier_from_env_file=True))


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://alice:FAKEPASSWORD@api.example.invalid/v1", "embeds credentials"),
        ("https://user@api.example.invalid/v1", "embeds credentials"),
        ("https://api.example.invalid/v1?api_key=FAKE0000FIXTURE", "query string"),
        ("https://api.example.invalid/v1#tokenFAKE", "query string"),
    ],
)
def test_a_base_url_carrying_credentials_is_withheld(
    tmp_path: Path, url: str, reason: str
) -> None:
    """Checked structurally with `urlparse`, not by guessing at keyword patterns."""

    runtime = build_runtime(tmp_path, provider="openai-compatible")
    console = resume_lines(runtime, environment=ResumeEnvironment(base_url=url))

    assert "FAKEPASSWORD" not in console.output
    assert "FAKE0000FIXTURE" not in console.output
    assert "alice" not in console.output
    assert "--base-url" not in console.command_line(), "the flag must not be emitted"
    assert console.has(reason)
    assert console.has("re-supply it manually")


def test_an_ordinary_base_url_is_shown_as_one_token(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, provider="openai-compatible")
    url = "https://api.example.invalid/compatible-mode/v1"
    console = resume_lines(runtime, environment=ResumeEnvironment(base_url=url))

    assert console.has("--base-url")
    assert quote_powershell(url) in console.command_line()


def test_a_base_url_with_an_ampersand_stays_one_token(tmp_path: Path) -> None:
    """An `&` must be inside the quoted string, never a PowerShell operator."""

    runtime = build_runtime(tmp_path, provider="openai-compatible")
    # No query, so it is displayed; the ampersand lives in the path.
    url = "https://api.example.invalid/a&b/v1"
    console = resume_lines(runtime, environment=ResumeEnvironment(base_url=url))

    line = console.command_line()
    assert quote_powershell(url) in line
    # The bare ampersand never appears outside a quoted string.
    assert " & " not in line


def test_a_hostile_value_cannot_produce_a_second_command(tmp_path: Path) -> None:
    """Metacharacters in real fields must not split the printed command."""

    data_dir = tmp_path / "a&b;c $(whoami)"
    runtime = build_runtime(tmp_path, data_dir=data_dir, model="m'x&y")
    console = resume_lines(runtime, session_id="s;1|2")

    line = console.command_line()
    assert "\n" not in line
    # Everything derived is inside a single-quoted literal.
    assert quote_powershell(str(data_dir.resolve())) in line
    assert quote_powershell("m'x&y") in line
    assert quote_powershell("s;1|2") in line


def test_a_value_with_control_characters_prints_no_command(tmp_path: Path) -> None:
    """Refusing to render beats rendering something that spans two lines."""

    runtime = build_runtime(tmp_path, model="bad\nmodel")
    console = resume_lines(runtime)

    assert not console.has("traceh chat")
    assert console.has("cannot be shown")
    # The session is still findable.
    assert console.has("session_id: s1")


# -- completeness and honesty -------------------------------------------


def test_the_command_says_it_is_not_a_full_snapshot(tmp_path: Path) -> None:
    console = resume_lines(build_runtime(tmp_path))

    assert console.has("not a complete configuration snapshot")


def test_a_named_plugin_verifier_is_preserved_in_the_resume_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        os,
        "environ",
        {key: value for key, value in os.environ.items() if not key.startswith("TRACEH_")},
    )
    runtime = build_runtime(tmp_path)
    runtime.config = replace(runtime.config, verifier_name="workspace-check")
    console = resume_lines(
        runtime,
        shell="posix",
        plugin_ids=("verification.extension",),
    )

    argv = shlex.split(console.command_line())
    resumed = build_parser().parse_args(argv[1:])
    _configure_from_environment(resumed)

    assert resumed.verifier_name == "workspace-check"
    assert resumed.plugins == ("verification.extension",)


def test_a_scripted_session_is_not_told_about_an_api_key(tmp_path: Path) -> None:
    """Naming OPENAI_API_KEY for a scripted run is a misleading instruction."""

    runtime = build_runtime(tmp_path, provider="scripted")
    console = resume_lines(
        runtime, environment=ResumeEnvironment(api_key_env="OPENAI_API_KEY")
    )

    assert not console.has("--api-key-env")
    assert "OPENAI_API_KEY" not in console.output


def test_an_openai_compatible_session_names_the_key_variable(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, provider="openai-compatible")
    console = resume_lines(
        runtime, environment=ResumeEnvironment(api_key_env="MY_PROVIDER_KEY")
    )

    assert console.has("--api-key-env")
    assert console.has("set MY_PROVIDER_KEY in that shell")
    assert console.has("value is never printed")
    assert "sk-" not in console.output


def test_a_key_supplied_by_the_env_file_is_described_as_such(tmp_path: Path) -> None:
    """Telling the user to set it in the shell would be wrong here."""

    env_file = tmp_path / ".env"
    env_file.write_text("MY_PROVIDER_KEY=FAKE\n", encoding="utf-8")
    runtime = build_runtime(tmp_path, provider="openai-compatible")
    console = resume_lines(
        runtime,
        environment=ResumeEnvironment(
            api_key_env="MY_PROVIDER_KEY",
            env_file=env_file,
            env_file_supplies=frozenset({"MY_PROVIDER_KEY"}),
        ),
    )

    assert console.has("available from that env file or the shell")
    assert not console.has("set MY_PROVIDER_KEY in that shell")
    assert "FAKE" not in console.output


def test_an_invalid_key_variable_name_is_not_emitted(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, provider="openai-compatible")
    console = resume_lines(
        runtime, environment=ResumeEnvironment(api_key_env="not a; var name")
    )

    assert not console.has("--api-key-env")


def test_an_explicit_script_is_carried_with_its_cursor_caveat(tmp_path: Path) -> None:
    """Omitting it would silently swap in the built-in placeholder provider."""

    script = tmp_path / "replies.json"
    script.write_text("[]", encoding="utf-8")
    runtime = build_runtime(tmp_path, provider="scripted")
    console = resume_lines(runtime, environment=ResumeEnvironment(script=script))

    assert console.has("--script")
    assert quote_powershell(str(script.resolve())) in console.command_line()
    assert console.has("replays the same file from its first response")
    assert console.has("cursor is not persisted")


def test_without_a_script_no_script_flag_is_printed(tmp_path: Path) -> None:
    console = resume_lines(build_runtime(tmp_path))

    assert not console.has("--script")


def test_the_env_file_is_named_only_when_it_was_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRACEH_MODEL=m\n", encoding="utf-8")
    runtime = build_runtime(tmp_path)

    loaded = resume_lines(runtime, environment=ResumeEnvironment(env_file=env_file))
    assert quote_powershell(str(env_file.resolve())) in loaded.command_line()

    absent = resume_lines(runtime)
    assert not absent.has("--env-file")


def test_locating_and_restoring_are_both_present(tmp_path: Path) -> None:
    """Finding the session and restoring its behaviour are different needs."""

    data_dir = tmp_path / "data dir"
    runtime = build_runtime(tmp_path, data_dir=data_dir, model="custom-model")
    console = resume_lines(runtime, session_id="sid-9")

    line = console.command_line()
    # Locating.
    assert quote_powershell("sid-9") in line
    assert quote_powershell(str(data_dir.resolve())) in line
    # Restoring.
    assert quote_powershell("scripted") in line
    assert quote_powershell("custom-model") in line
    assert console.has("traceh sessions")


async def test_the_resume_command_reproduces_provider_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running it elsewhere must not silently change the model.

    The original session took its model from a `.env` in its working directory,
    so a command carrying only the session id re-resolved configuration from
    wherever it ran and fell back to the default.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRACEH_PROVIDER=scripted\nTRACEH_MODEL=custom-model\n", encoding="utf-8"
    )

    original = build_parser().parse_args(
        ["chat", str(workspace), "--env-file", str(env_file)]
    )
    original.env_report = _configure_from_environment(original)
    assert original.model == "custom-model"

    runtime = build_runtime(
        tmp_path,
        data_dir=tmp_path / "data dir",
        provider=original.provider,
        model=original.model,
        max_steps=original.max_steps,
    )
    # POSIX so the rendered command can be parsed back with `shlex`.
    console = resume_lines(
        runtime,
        session_id="s-resume",
        environment=ResumeEnvironment(
            base_url=original.base_url,
            api_key_env=original.api_key_env,
            env_file=original.env_report.path if original.env_report.loaded else None,
        ),
        shell="posix",
    )
    argv = shlex.split(console.command_line())
    assert argv[0] == "traceh"

    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        os,
        "environ",
        {key: value for key, value in os.environ.items() if not key.startswith("TRACEH_")},
    )

    resumed = build_parser().parse_args(argv[1:])
    _configure_from_environment(resumed)

    assert resumed.provider == original.provider
    assert resumed.model == "custom-model", "the resumed session changed model"
    assert resumed.session_id == "s-resume"
    assert Path(resumed.data_dir) == (tmp_path / "data dir")


def test_safe_base_url_reports_why_it_withheld() -> None:
    assert _safe_base_url(None) == (None, None)
    assert _safe_base_url("https://h/v1") == ("https://h/v1", None)
    shown, reason = _safe_base_url("https://u:p@h/v1")
    assert shown is None and "credentials" in reason
    shown, reason = _safe_base_url("https://h/v1?k=v")
    assert shown is None and "query" in reason
