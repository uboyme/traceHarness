"""What may appear on one line of terminal output.

Several places need the same answer - the shell command renderer, the resume
block's fallback, the base URL check and the timeline - and they had drifted onto
their own copies of "control characters". Each copy tested the Unicode `C*`
categories, and each therefore missed `U+2028 LINE SEPARATOR` and
`U+2029 PARAGRAPH SEPARATOR`, which are categories `Zl` and `Zp`.

Those two are line breaks to plenty of consumers - `str.splitlines()` splits on
them, as do many editors, log viewers and renderers - so a value containing one
could still turn a single row into two:

    escape_for_display("x\\u2028note: forged").splitlines()
    -> ["x", "note: forged"]

This module holds only the character-class question. It deliberately knows
nothing about shell syntax or event payloads: the shell renderer and the timeline
each apply their own policy on top of the same predicate, rather than importing
each other.
"""

from __future__ import annotations

import unicodedata

#: Unicode general categories that must never reach a terminal line unescaped.
#:
#: * ``Cc`` - C0/C1 controls: NUL, ESC, CR, LF, backspace;
#: * ``Cf`` - format characters, including the bidirectional overrides that
#:   visually reorder a line and the zero-width joiners that hide inside one;
#: * ``Cs``/``Co`` - surrogates and private use, which no terminal can be
#:   trusted to render predictably;
#: * ``Zl``/``Zp`` - the explicit line and paragraph separators, which are the
#:   ones a `C*`-only check silently lets through.
UNSAFE_LINE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def is_unsafe_character(character: str) -> bool:
    """Whether one character could break or disguise a single line of output."""

    return unicodedata.category(character) in UNSAFE_LINE_CATEGORIES


def is_single_line_safe(value: str) -> bool:
    """Whether `value` can appear on one terminal line exactly as written."""

    return not any(is_unsafe_character(character) for character in value)
