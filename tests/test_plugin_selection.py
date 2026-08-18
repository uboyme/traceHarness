"""Explicit enablement: installation never activates a plugin.

Selection is validated before discovery, so a malformed id cannot reach any
third-party code, and the rejected text is never echoed back.
"""

from __future__ import annotations

import pytest

from traceh.plugins.errors import PluginValidationError
from traceh.plugins.selection import is_plugin_id, resolve_enabled_plugins


def test_nothing_enabled_by_default() -> None:
    assert resolve_enabled_plugins(None, None) == ()
    assert resolve_enabled_plugins([], None) == ()
    assert resolve_enabled_plugins(None, "") == ()


def test_environment_variable_is_comma_separated() -> None:
    assert resolve_enabled_plugins(None, "a.one,b.two") == ("a.one", "b.two")


def test_environment_values_are_trimmed() -> None:
    assert resolve_enabled_plugins(None, " a.one , b.two ") == ("a.one", "b.two")


def test_cli_occurrences_replace_the_environment_entirely() -> None:
    """--plugin replaces TRACEH_PLUGINS rather than adding to it."""

    assert resolve_enabled_plugins(["only.this"], "a.one,b.two") == ("only.this",)


def test_cli_preserves_given_order() -> None:
    assert resolve_enabled_plugins(["z.one", "a.two"], None) == ("z.one", "a.two")


@pytest.mark.parametrize("value", ["", "   ", ",", "a.one,,b.two"])
def test_empty_ids_are_rejected(value: str) -> None:
    with pytest.raises(PluginValidationError) as info:
        resolve_enabled_plugins(None, value or ",")
    assert any(failure.code == "empty-plugin-id" for failure in info.value.failures)


@pytest.mark.parametrize(
    "value",
    [
        "Upper.Case",
        "has space",
        "trailing-",
        "-leading",
        "has/slash",
        "has;semicolon",
        "has\nnewline",
        "has\x1b[2Jescape",
        "has‮override",
        "a" * 200,
    ],
)
def test_invalid_ids_are_rejected(value: str) -> None:
    with pytest.raises(PluginValidationError) as info:
        resolve_enabled_plugins([value], None)
    assert any(failure.code == "invalid-plugin-id" for failure in info.value.failures)


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(PluginValidationError) as info:
        resolve_enabled_plugins(["a.one", "a.one"], None)
    assert any(failure.code == "duplicate-plugin-id" for failure in info.value.failures)


def test_rejected_value_is_never_echoed_back() -> None:
    """The commonest way to mis-set this is pasting a token into it."""

    secret = "sk-proj-FAKE-FIXTURE-NOT-A-REAL-KEY"
    with pytest.raises(PluginValidationError) as info:
        resolve_enabled_plugins([secret], None)
    rendered = str(info.value) + "".join(f.message for f in info.value.failures)
    assert secret not in rendered
    assert "FAKE" not in rendered
    # No derivable hints either: no length, no prefix, no suffix.
    assert str(len(secret)) not in rendered


def test_error_messages_stay_single_line_and_control_free() -> None:
    with pytest.raises(PluginValidationError) as info:
        resolve_enabled_plugins(["bad\nname\x1b[31m"], None)
    for failure in info.value.failures:
        assert failure.message.splitlines() == [failure.message]
        assert "\x1b" not in failure.message


@pytest.mark.parametrize("value", ["a", "a.b", "a-b", "a_b", "a.b-c_d.9", "x9"])
def test_valid_ids_accepted(value: str) -> None:
    assert is_plugin_id(value)
    assert resolve_enabled_plugins([value], None) == (value,)


@pytest.mark.parametrize("value", ["", "A", "a b", ".a", "a."])
def test_is_plugin_id_rejects(value: str) -> None:
    assert not is_plugin_id(value)
