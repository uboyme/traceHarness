"""Terminal input/output boundary for the interactive CLI.

Two jobs, deliberately separated from the chat logic itself:

* make text I/O behave predictably for non-ASCII input on every platform,
  Windows PowerShell included;
* give the chat loop an injectable seam so tests never need a real keyboard.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

# U+FFFD, produced by a decoder when bytes cannot be decoded. Its presence means
# the original characters are already lost, so guessing them back is impossible.
REPLACEMENT_CHARACTER = "�"

# U+FEFF. Some tools prefix a byte-order mark and the plain utf-8 codec keeps it -
# Windows PowerShell 5.1 does this with `Out-File -Encoding utf8`, while
# PowerShell 7's `utf8` is BOM-less. It belongs to the stream, never to the
# message.
BYTE_ORDER_MARK = "﻿"


@dataclass(frozen=True, slots=True)
class Console:
    """The only way the chat loop touches the terminal.

    ``read_line`` receives the prompt, returns one line without its trailing
    newline, and raises ``EOFError`` when the input ends.
    """

    read_line: Callable[[str], str]
    write: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class StdioReport:
    """Which standard streams accepted the UTF-8 reconfiguration."""

    stdin: bool
    stdout: bool
    stderr: bool


def configure_stdio(
    *,
    stdin: object | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
) -> StdioReport:
    """Read and write the standard streams as UTF-8.

    The policy is uniform and explainable rather than platform-specific: all
    three streams use UTF-8 with ``errors="replace"``. Output therefore never
    dies on a character the terminal cannot encode, and undecodable *input*
    turns into :data:`REPLACEMENT_CHARACTER` instead of silently mutating into
    some other character - which lets the caller refuse it, see
    :func:`contains_undecodable_input`.

    Streams that cannot be reconfigured (a ``StringIO`` in a test, an already
    detached stream) are left alone instead of raising; the report says what
    actually happened.
    """

    return StdioReport(
        stdin=_reconfigure(sys.stdin if stdin is None else stdin),
        stdout=_reconfigure(sys.stdout if stdout is None else stdout),
        stderr=_reconfigure(sys.stderr if stderr is None else stderr),
    )


def _reconfigure(stream: object) -> bool:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, LookupError, OSError, ValueError):
        return False
    return True


def contains_undecodable_input(text: str) -> bool:
    """Whether the line carries characters that were already lost while decoding."""

    return REPLACEMENT_CHARACTER in text


def normalize_input(text: str) -> str:
    """Turn one raw line into the text the user actually meant.

    Only stream artefacts are removed - a leading byte-order mark and the
    surrounding whitespace of the prompt. Everything else, non-ASCII included,
    is left exactly as typed.
    """

    return text.lstrip(BYTE_ORDER_MARK).strip()


def default_console() -> Console:
    """The real terminal console."""

    def read_line(prompt: str) -> str:
        return input(prompt)

    def write(text: str) -> None:
        print(text)

    return Console(read_line=read_line, write=write)
