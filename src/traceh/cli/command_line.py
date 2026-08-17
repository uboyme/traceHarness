"""Render a command line that is safe to paste into a specific shell.

A resume hint is printed for the user to copy, which makes it a place where
untrusted text - a session id, a workspace path, a model name from a `.env` -
becomes something a shell will parse. Concatenating those values and quoting
only when they contain a space is not enough: PowerShell treats ``&``, ``;``,
``|``, ``$(...)`` and the backtick as syntax outside quotes, so an unquoted
value can end the command and start another one.

Two rules make that impossible here.

**Build tokens, then render.** Callers assemble a list of literal argv values and
never build command text themselves. Rendering to shell syntax happens once, in
one place, for one named shell.

**One quoting rule per shell, never a shared one.** PowerShell and POSIX have
different literal-string rules, and pretending otherwise is how a value ends up
quoted for the wrong parser. Each renderer implements its own, and the printed
block says which shell it is for.

Values carrying control characters are refused rather than escaped: a newline in
a rendered command would produce a second command line, and no quoting rule
should have to be trusted to prevent that. What counts as one of those characters
comes from `cli/text_safety.py`, which includes `U+2028`/`U+2029` - line breaks a
`C*`-categories check silently lets through.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable, Sequence

from traceh.cli.text_safety import is_single_line_safe, is_unsafe_character


class UnsafeCommandValue(ValueError):
    """A value cannot be represented as a single, single-line shell token."""


class Literal(str):
    """A token authored in this codebase rather than derived from input.

    Program names, subcommands and flag names are marked with this so they can be
    rendered bare. That is not cosmetic: PowerShell parses a quoted string at the
    start of a statement as an *expression*, so ``'traceh' 'chat'`` prints the word
    instead of running anything - a quoted command name silently produces a
    command that does nothing.

    A literal is still validated before being emitted bare, so a mistaken marker
    degrades into quoting rather than into injection.
    """


#: Characters safe to emit unquoted in both supported shells. Deliberately
#: excludes ``&``, ``;``, ``|``, ``$``, backtick, quotes, parentheses, braces and
#: ``@`` - each of which is syntax to at least one of them.
_BARE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./:=+"
)


def _is_bare_safe(value: str) -> bool:
    return bool(value) and all(character in _BARE for character in value)


def is_renderable(value: str) -> bool:
    """Whether `value` can appear in a rendered command at all.

    Uses the shared single-line rule, so the line and paragraph separators
    (`U+2028`/`U+2029`) are refused alongside the control characters. A
    categories-`C*`-only check let those through, and they are line breaks to
    enough consumers to split one command into two.
    """

    return is_single_line_safe(value)


def _check(value: str) -> str:
    if not is_renderable(value):
        raise UnsafeCommandValue(
            "command values cannot contain control characters or line breaks"
        )
    return value


def escape_for_display(value: str, *, limit: int = 200) -> str:
    """Render an untrusted value as one printable line, for prose rather than argv.

    Used where a command cannot be produced but the user still needs to see what
    was involved. Refusing to render (as `_check` does) would leave them with
    nothing to go on; printing the raw value would put the control characters
    back on the terminal - which is how a value containing a newline produced a
    second line in the very block that was explaining it could not be shown.

    Control, format and line-separator characters become their visible escape
    spelling, so the output is informative and inert at the same time. That
    includes `U+2028`/`U+2029`, which render as ``\\u2028``/``\\u2029``: they are
    line breaks to `str.splitlines()` and to many viewers, so leaving them intact
    would let a value forge a second row inside this very explanation.
    """

    pieces: list[str] = []
    for character in value:
        if character in _DISPLAY_ESCAPES:
            pieces.append(_DISPLAY_ESCAPES[character])
        elif is_unsafe_character(character):
            code = ord(character)
            pieces.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            pieces.append(character)
    escaped = "".join(pieces)
    if len(escaped) > limit:
        escaped = escaped[: limit - 1] + "…"
    return escaped


#: Escapes worth spelling out by name rather than by code point.
_DISPLAY_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\x00": "\\0"}


def quote_powershell(value: str) -> str:
    """Quote one value as a PowerShell *literal* string.

    Single quotes are PowerShell's non-interpolating form: inside them ``$(...)``,
    ``$var``, the backtick escape, ``&``, ``;`` and ``|`` are ordinary text. The
    only character needing attention is the single quote itself, which is escaped
    by doubling it - PowerShell's own rule, not a backslash.
    """

    return "'" + _check(value).replace("'", "''") + "'"


def render_powershell(tokens: Sequence[str]) -> str:
    """Render argv for PowerShell.

    Every value is quoted; only a `Literal` that is provably free of shell syntax
    is emitted bare, which keeps the command runnable and readable without
    letting any derived value reach the parser unquoted.
    """

    rendered = []
    for token in tokens:
        if isinstance(token, Literal) and _is_bare_safe(token):
            rendered.append(_check(token))
        else:
            rendered.append(quote_powershell(token))
    return " ".join(rendered)


def render_posix(tokens: Sequence[str]) -> str:
    """Render argv for a POSIX shell using the standard library's own rule."""

    rendered = []
    for token in tokens:
        if isinstance(token, Literal) and _is_bare_safe(token):
            rendered.append(_check(token))
        else:
            rendered.append(shlex.quote(_check(token)))
    return " ".join(rendered)


#: Renderers by the shell name shown to the user.
RENDERERS: dict[str, Callable[[Sequence[str]], str]] = {
    "powershell": render_powershell,
    "posix": render_posix,
}


def default_shell(platform: str | None = None) -> str:
    """The shell a copied command will most likely be pasted into."""

    return "powershell" if (platform or sys.platform).startswith("win") else "posix"


def render_command(tokens: Sequence[str], *, shell: str | None = None) -> str:
    """Render argv for `shell`, defaulting to the one this platform implies."""

    name = shell or default_shell()
    try:
        renderer = RENDERERS[name]
    except KeyError:  # pragma: no cover - guarded by callers
        raise ValueError(f"unknown shell: {name!r}") from None
    return renderer(tokens)
