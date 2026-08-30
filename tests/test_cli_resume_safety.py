"""Robustness of the resume command's safety checks.

Separated from `test_cli_resume.py` because these are about the checks failing
safely rather than about what the command contains: an input that crashes the
credential check, a value that cannot be rendered at all, and a configuration
value that must be rejected instead of quietly dropped.

Every credential-shaped string is a marked test fixture - `FAKE`/`FIXTURE` and
`.invalid` hosts - never a real secret.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from traceh.api.llm import ModelResponse
from traceh.chat.session import ChatSession
from traceh.cli.chat import ResumeEnvironment, _safe_base_url, _write_resume_block
from traceh.cli.command_line import (
    Literal,
    UnsafeCommandValue,
    escape_for_display,
    is_renderable,
    render_posix,
    render_powershell,
)
from traceh.cli.console import Console
from traceh.cli.errors import CliConfigurationError
from traceh.cli.main import _configure_from_environment, build_parser
from traceh.cli.text_safety import UNSAFE_LINE_CATEGORIES as UNSAFE
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


class StubOpenAiCompatible:
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
        return next(
            (line for line in self.lines if "traceh chat" in line), ""
        ).strip()


def build_runtime(
    tmp_path: Path,
    *,
    data_dir: Path | None = None,
    provider: str = "scripted",
    model: str = "qwen-plus",
):
    return build_default_runtime(
        RuntimeConfig(
            data_dir=data_dir or (tmp_path / ".traceh"),
            provider=provider,
            model=model,
        ),
        provider=(
            StubOpenAiCompatible()
            if provider == "openai-compatible"
            else ScriptedLlmProvider((ModelResponse(content="hi"),), repeat_last=True)
        ),
        event_store=InMemoryEventStore(),
    )


def resume_lines(runtime, session_id="s1", *, environment=None, shell="powershell"):
    console = FakeConsole()
    _write_resume_block(
        runtime,
        console.console,
        ChatSession(session_id, Path(".")),
        environment or ResumeEnvironment(),
        shell=shell,
        plugin_ids=runtime.enabled_plugin_ids,
    )
    return console


def assert_single_inert_line(line: str) -> None:
    """One printable row: no control characters, no forged second line.

    `splitlines()` is the load-bearing check. Testing only for `\\n`/`\\r` and the
    `C*` categories passed happily on `U+2028`/`U+2029`, which are categories
    `Zl`/`Zp` and *are* line breaks to `splitlines()` and to many viewers - so a
    value could still turn one row into two while every assertion held.
    """

    assert len(line.splitlines()) <= 1, f"forged row: {line!r}"
    assert "\n" not in line and "\r" not in line, f"forged row: {line!r}"
    for separator in ("\u2028", "\u2029"):
        assert separator not in line, f"line separator survived: {line!r}"
    offenders = [
        character for character in line if unicodedata.category(character) in UNSAFE
    ]
    assert not offenders, f"unsafe characters survived: {offenders!r} in {line!r}"


def isolated_argv(tmp_path: Path, *extra: str) -> list[str]:
    """Argv that cannot pick up a real `.env` from the working directory.

    Without this the suite reads whatever `.env` happens to sit in the repo, so a
    developer's own provider settings would decide whether a test passes.
    """

    return ["chat", ".", "--env-file", str(tmp_path / "absent.env"), *extra]


def clean_environ(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {
            **{key: value for key, value in os.environ.items() if not key.startswith("TRACEH_")},
            **extra,
        },
    )


# -- the base URL check must never crash the chat ------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",
        "https://[::1",
        "http://[",
        "https://[not:an:ipv6]:99999/v1",
        "https://[v1.fe80::a+en1",
    ],
)
def test_an_unparseable_base_url_is_withheld_rather_than_raising(url: str) -> None:
    """`urlparse` raises on some inputs; formatting a hint must not kill the chat.

    `https://[bad` fails with "Invalid IPv6 URL" only once the netloc is inspected
    for userinfo - which is exactly what the credential check does.
    """

    shown, reason = _safe_base_url(url)

    assert shown is None, "an unparseable URL must not be displayed"
    assert reason, "a withheld URL must say why"
    assert url not in reason, "the reason must not echo the URL"


def test_an_unparseable_base_url_still_produces_a_usable_block(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, provider="openai-compatible")
    console = resume_lines(runtime, environment=ResumeEnvironment(base_url="https://[bad"))

    assert "--base-url" not in console.command_line()
    assert console.has("could not be parsed")
    assert console.has("re-supply it manually")
    assert "[bad" not in console.output
    # The rest of the command is unaffected.
    assert console.has("traceh chat")


def test_product_configuration_is_carried_into_the_chat_resume_command(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    config = tmp_path / "product host.json"
    console = resume_lines(
        runtime,
        environment=ResumeEnvironment(product_config=config),
    )

    assert "--product-config" in console.command_line()
    assert str(config.resolve()) in console.command_line()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://u:FAKEPW@h.invalid/v1", "embeds credentials"),
        ("https://u@h.invalid/v1", "embeds credentials"),
        ("https://h.invalid/v1?token=FAKE0000", "query string"),
        ("https://h.invalid/v1#FAKE0000", "query string"),
        ("https://h.invalid/v1\n", "control characters"),
        ("https://h.invalid/v1\x1b[2J", "control characters"),
        ("https://h.invalid/\u202ev1", "control characters"),
    ],
)
def test_every_withheld_base_url_reports_a_reason_without_echoing_it(
    url: str, expected: str
) -> None:
    shown, reason = _safe_base_url(url)

    assert shown is None
    assert expected in reason
    for secret in ("FAKEPW", "FAKE0000"):
        assert secret not in reason


# -- the fallback when nothing can be rendered --------------------------


HOSTILE_VALUES = [
    "line\nbreak",
    "esc\x1b[2Jclear",
    "bidi\u202eflip",
    "nul\x00byte",
    # Categories Zl/Zp: line breaks that a `C*`-only check lets straight through,
    # each carrying text shaped like one of our own rows.
    "sep\u2028note: forged",
    "para\u2029traceh chat --session-id evil",
]


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
@pytest.mark.parametrize("field", ["session_id", "data_dir", "model"])
def test_the_fallback_escapes_every_value_it_shows(
    tmp_path: Path, field: str, hostile: str
) -> None:
    """The value that prevented rendering must not break the explanation either.

    Printing it raw is how a data dir containing a newline produced a second
    terminal line inside the very block saying no command could be printed.
    """

    if field == "data_dir" and "\x00" in hostile:
        # `pathlib` rejects a NUL before any rendering happens, and no real
        # filesystem can hold such a directory; the other fields cover it.
        pytest.skip("a path cannot contain NUL")

    runtime = build_runtime(
        tmp_path,
        data_dir=(tmp_path / hostile) if field == "data_dir" else None,
        model=hostile if field == "model" else "qwen-plus",
    )
    console = resume_lines(runtime, session_id=hostile if field == "session_id" else "s1")

    # The hostile value may legitimately contain the words "traceh chat"; what
    # must not exist is a *line that is* a command.
    assert not any(
        line.strip().startswith("traceh chat") for line in console.lines
    ), "no command may be rendered"
    assert console.has("cannot be shown")
    assert console.has("escaped for display")
    for line in console.lines:
        assert_single_inert_line(line)


def test_the_fallback_cannot_forge_extra_command_note_or_event_rows(
    tmp_path: Path,
) -> None:
    """A crafted value must not add a row that looks like one of ours."""

    hostile = "s1\n  traceh chat --session-id evil\n  note: fake\n[event 1] fake"
    runtime = build_runtime(tmp_path, model="m\nbad")
    console = resume_lines(runtime, session_id=hostile)

    for line in console.lines:
        assert_single_inert_line(line)
    assert not any(line.strip().startswith("traceh chat") for line in console.lines)
    assert sum(1 for line in console.lines if line.strip().startswith("note:")) == 1
    assert not any(line.strip().startswith("[event ") for line in console.lines)
    # The injected text survives only as inert, escaped content of one row.
    assert console.has("session_id: s1\\n")


def test_the_fallback_still_shows_where_the_session_is(tmp_path: Path) -> None:
    """Refusing to render must not leave the user with nothing to go on."""

    runtime = build_runtime(tmp_path, model="m\nbad")
    console = resume_lines(runtime, session_id="findable-id")

    assert console.has("findable-id")
    assert console.has("data dir:")


def test_escape_for_display_is_inert_and_bounded() -> None:
    escaped = escape_for_display("a\nb\rc\x1bd\u202ee\x00f" + "z" * 500)

    assert_single_inert_line(escaped)
    assert escaped.startswith("a\\nb\\rc\\x1bd\\u202ee\\0f")
    assert len(escaped) <= 200


def test_escape_for_display_leaves_ordinary_text_alone() -> None:
    assert escape_for_display("读取 hello.txt") == "读取 hello.txt"
    assert escape_for_display("C:/a b/c-d_e.txt") == "C:/a b/c-d_e.txt"


# -- environment variable name validation --------------------------------


@pytest.mark.parametrize(
    "name", ["bad;name", "bad name", "1leading", "with-dash", "", "a$b", "x\ny"]
)
def test_an_unusable_key_variable_name_is_rejected_at_configuration_time(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting it and dropping it later let the next run use a different variable."""

    clean_environ(monkeypatch)
    args = build_parser().parse_args(isolated_argv(tmp_path, "--api-key-env", name))

    with pytest.raises(CliConfigurationError, match="NAME of an environment variable"):
        _configure_from_environment(args)


def test_an_unusable_key_variable_name_from_the_env_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule applies wherever the value came from."""

    clean_environ(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("TRACEH_API_KEY_ENV=bad;name\n", encoding="utf-8")
    args = build_parser().parse_args(["chat", str(tmp_path), "--env-file", str(env_file)])

    with pytest.raises(CliConfigurationError, match="NAME of an environment variable"):
        _configure_from_environment(args)


def test_a_scripted_provider_does_not_excuse_an_unusable_key_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignoring the key at runtime does not make an unusable name valid.

    The rule is the same for every provider, so a session cannot be started with
    a configuration that a later `openai-compatible` run would reject.
    """

    clean_environ(monkeypatch)
    args = build_parser().parse_args(
        isolated_argv(tmp_path, "--provider", "scripted", "--api-key-env", "bad;name")
    )

    with pytest.raises(CliConfigurationError, match="NAME of an environment variable"):
        _configure_from_environment(args)


def test_the_rejection_message_does_not_echo_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_environ(monkeypatch)
    args = build_parser().parse_args(
        isolated_argv(tmp_path, "--api-key-env", "bad\x1b[2Jname")
    )

    with pytest.raises(CliConfigurationError) as error:
        _configure_from_environment(args)

    message = str(error.value)
    assert_single_inert_line(message)
    # The value is no longer shown at all - not even escaped - so the message
    # contains no fragment of it.
    assert "bad" not in message
    assert "2J" not in message


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "MY_PROVIDER_KEY", "_underscore", "K1"])
def test_a_valid_custom_key_variable_name_still_works(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_environ(monkeypatch)
    args = build_parser().parse_args(isolated_argv(tmp_path, "--api-key-env", name))

    _configure_from_environment(args)

    assert args.api_key_env == name


def test_the_default_key_variable_name_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built-in default must satisfy the rule it enforces."""

    clean_environ(monkeypatch)
    args = build_parser().parse_args(isolated_argv(tmp_path))

    _configure_from_environment(args)

    assert args.api_key_env == "OPENAI_API_KEY"


# -- line and paragraph separators (categories Zl/Zp) --------------------
#
# These are the ones a `C*`-categories check misses. They are line breaks to
# `str.splitlines()` and to many viewers, so a value carrying one could split a
# single row - or a single command - into two while every older assertion held.

SEPARATORS = ["\u2028", "\u2029"]


@pytest.mark.parametrize("separator", SEPARATORS)
def test_a_line_separator_is_not_renderable(separator: str) -> None:
    assert not is_renderable(f"value{separator}second")


@pytest.mark.parametrize("separator", SEPARATORS)
def test_both_shells_refuse_a_line_separator(separator: str) -> None:
    """Neither quoting rule may be trusted to contain a line break."""

    hostile = f"x{separator}traceh chat --session-id evil"
    for renderer in (render_powershell, render_posix):
        with pytest.raises(UnsafeCommandValue):
            renderer([Literal("traceh"), hostile])


@pytest.mark.parametrize("separator", SEPARATORS)
def test_escape_for_display_spells_out_a_line_separator(separator: str) -> None:
    escaped = escape_for_display(f"a{separator}b")

    assert_single_inert_line(escaped)
    expected = "a" + "\\u" + f"{ord(separator):04x}" + "b"
    assert escaped == expected
    assert len(escaped.splitlines()) == 1


@pytest.mark.parametrize("separator", SEPARATORS)
def test_a_base_url_with_a_line_separator_is_withheld(separator: str) -> None:
    shown, reason = _safe_base_url(f"https://h.invalid/v1{separator}evil")

    assert shown is None
    assert "control characters" in reason


@pytest.mark.parametrize("separator", SEPARATORS)
def test_the_fallback_cannot_be_split_by_a_line_separator(
    tmp_path: Path, separator: str
) -> None:
    """The classic shape: a value that looks like one of our own rows."""

    hostile = f"s1{separator}  note: forged{separator}  traceh chat --session-id evil"
    runtime = build_runtime(tmp_path, model=f"m{separator}bad")
    console = resume_lines(runtime, session_id=hostile)

    for line in console.lines:
        assert_single_inert_line(line)
    assert not any(line.strip().startswith("traceh chat") for line in console.lines)
    assert sum(1 for line in console.lines if line.strip().startswith("note:")) == 1
    assert not any(line.strip().startswith("[event ") for line in console.lines)
    # Everything the console produced is still one row per write.
    assert len("\n".join(console.lines).splitlines()) == len(console.lines)


@pytest.mark.parametrize("separator", SEPARATORS)
def test_the_shared_category_set_covers_the_separators(separator: str) -> None:
    """Both call sites read one set, so they cannot drift apart again."""

    assert unicodedata.category(separator) in UNSAFE


# -- a rejected variable name is never echoed ---------------------------

#: The realistic mistake: pasting the key where the variable *name* belongs.
#: NOT real credentials - each carries a FAKE/FIXTURE marker.
#: Only shapes that are *also* invalid identifiers belong here. A pasted key that
#: happens to be a valid identifier is accepted - see the boundary test at the end
#: of this file.
PASTED_SECRETS = [
    "sk-proj-FAKE0000FIXTURE",
    "xoxb-FAKE-0000-FIXTURE",
    "FAKE0000FIXTURE=trailing",
    "FAKE 0000 FIXTURE",
]


@pytest.mark.parametrize("secret", PASTED_SECRETS)
def test_a_rejected_variable_name_is_never_echoed(
    secret: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escaping defends against control characters, not against a printable secret.

    The usual way this setting is got wrong is by pasting the key itself instead
    of the name of the variable holding it - so the invalid value is precisely
    the thing that must not be reported.
    """

    clean_environ(monkeypatch)
    args = build_parser().parse_args(isolated_argv(tmp_path, "--api-key-env", secret))

    with pytest.raises(CliConfigurationError) as error:
        _configure_from_environment(args)

    message = str(error.value)
    assert secret not in message
    for fragment in ("FAKE", "FIXTURE", "sk-", "ghp_", "xoxb", "AKIA"):
        assert fragment not in message
    # Nor anything that narrows a guess about it.
    assert str(len(secret)) not in message
    assert secret[:4] not in message
    assert secret[-4:] not in message
    # It still says what to do.
    assert "TRACEH_API_KEY_ENV" in message or "--api-key-env" in message
    assert "letters, digits and underscore" in message


@pytest.mark.parametrize(
    "hostile",
    ["bad\x1b[2Jname", "bad\nname", "bad\u2028name", "bad\u2029name", "bad\u202ename"],
)
def test_a_rejected_name_with_hostile_characters_stays_one_safe_line(
    hostile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_environ(monkeypatch)
    args = build_parser().parse_args(isolated_argv(tmp_path, "--api-key-env", hostile))

    with pytest.raises(CliConfigurationError) as error:
        _configure_from_environment(args)

    message = str(error.value)
    assert_single_inert_line(message)
    assert "bad" not in message


#: Left-hand sides that a `.env` file cannot accept. `NAME=value` shapes are
#: excluded: `FAKE=trailing` parses as a perfectly valid assignment.
PASTED_SECRETS_IN_ENV_FILE = [
    "sk-proj-FAKE0000FIXTURE",
    "xoxb-FAKE-0000-FIXTURE",
    "FAKE 0000 FIXTURE",
]


@pytest.mark.parametrize("secret", PASTED_SECRETS_IN_ENV_FILE)
def test_an_env_file_with_a_pasted_secret_as_a_name_does_not_echo_it(
    secret: str, tmp_path: Path
) -> None:
    """A malformed left-hand side can hold a secret just as easily as the value."""

    from traceh.cli.env_file import EnvFileError, parse_env_file

    with pytest.raises(EnvFileError) as error:
        parse_env_file(f"{secret}=value\n")

    message = str(error.value)
    assert secret not in message
    for fragment in ("FAKE", "FIXTURE", "sk-", "ghp_", "xoxb", "AKIA"):
        assert fragment not in message
    assert_single_inert_line(message)
    # The line number is enough to find it.
    assert "line 1" in message


def test_a_secret_shaped_like_an_identifier_is_accepted_and_that_is_a_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stated limit, pinned so it is not mistaken for protection.

    The check is structural: it asks whether the value *is* a usable variable
    name, which it cannot distinguish from a key that happens to look like one.
    A pasted `ghp_...` or `AKIA...` is a valid identifier, so it is accepted -
    and, being the configured variable name, it is then shown in the resume
    command. Rejecting every identifier that resembles a credential would reject
    legitimate names such as `GH_TOKEN`; the honest position is that this
    validates shape, not intent.
    """

    clean_environ(monkeypatch)
    for identifier_shaped in ("ghp_FAKE0000FIXTURE00", "AKIAFAKE0000FIXTURE"):
        args = build_parser().parse_args(
            isolated_argv(tmp_path, "--api-key-env", identifier_shaped)
        )
        _configure_from_environment(args)
        assert args.api_key_env == identifier_shaped
