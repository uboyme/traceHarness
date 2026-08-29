"""Session plugin identity: PEP 440 equivalence, and a truly reserved key.

Two defects are pinned down here.

**Version comparison.** The identity check normalised each version with
``str(Version(...))`` and then compared strings. That does not do what it looks
like it does: ``str(Version("1.0"))`` is ``"1.0"`` while ``str(Version("1.0.0"))``
is ``"1.0.0"``, so two PEP 440 *equivalent* versions were reported as a
composition change and the session was refused.

**Reserved metadata key.** ``traceh_plugins`` was rejected only when the supplied
value differed from the expected one, so ``[]``, ``None`` and an exactly matching
list all slipped through - the caller could write a key that is supposed to
record only what the runtime itself observed.

These use real sessions and the real event log, not hand-built metadata dicts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from plugin_fixtures import RecordingTool, ScriptedPlugin, entry_point_for, manifest, provider_for

from traceh.api.prompts import PromptSection
from traceh.plugins.discovery import PluginDiscovery
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    SessionPluginMismatchError,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.session.sqlite import SqliteEventStore


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "ws"
    directory.mkdir()
    (directory / "hello.txt").write_text("hi", encoding="utf-8")
    return directory


def plugin_at(version: str) -> ScriptedPlugin:
    return ScriptedPlugin(
        manifest("a.example", version=version),
        tools=(RecordingTool("plugin_tool"),),
        prompts=(PromptSection("a.example.section", "Guidance.", 40),),
    )


async def runtime_with(data_dir: Path, version: str):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=PluginDiscovery(
            entry_points_provider=provider_for(entry_point_for(plugin_at(version)))
        ),
        event_store=SqliteEventStore((data_dir) / "events"),
    )


async def session_created_under(data_dir: Path, workspace: Path, version: str) -> str:
    runtime = await runtime_with(data_dir, version)
    try:
        return await runtime.create_session(workspace)
    finally:
        await runtime.dispose()


# --------------------------------------------------------------------------
# PEP 440 equivalence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "active"),
    [
        ("1.0", "1.0.0"),
        ("1.0.0", "1.0"),
        ("1.0", "1.0.0.0"),
        ("1.0.0", "1.0.0.0"),
        ("2.0", "2.0.0"),
        ("1.0.0", "1.0.0"),
    ],
)
async def test_equivalent_versions_can_continue_the_session(
    tmp_path: Path, workspace: Path, recorded: str, active: str
) -> None:
    """A real session created under `recorded` must continue under `active`."""

    data_dir = tmp_path / "data"
    session_id = await session_created_under(data_dir, workspace, recorded)

    runtime = await runtime_with(data_dir, active)
    try:
        await runtime.verify_session_plugins(session_id)
        result = await runtime.run_existing(session_id, "continue")
        assert result.reason == "completed"
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("recorded", "active"),
    [
        ("1.0", "1.0.1"),
        ("1.0.0", "1.0.1"),
        ("1.0", "1.1"),
        ("1.0", "2.0"),
        ("2.0.0", "1.0.0"),
        ("1.0", "1.0.post1"),
    ],
)
async def test_different_versions_still_refuse_the_session(
    tmp_path: Path, workspace: Path, recorded: str, active: str
) -> None:
    data_dir = tmp_path / "data"
    session_id = await session_created_under(data_dir, workspace, recorded)

    runtime = await runtime_with(data_dir, active)
    try:
        with pytest.raises(SessionPluginMismatchError):
            await runtime.verify_session_plugins(session_id)
    finally:
        await runtime.dispose()


async def test_mismatch_message_reports_what_the_session_recorded(
    tmp_path: Path, workspace: Path
) -> None:
    """The message keeps the original text, not a normalised rewrite of it."""

    data_dir = tmp_path / "data"
    session_id = await session_created_under(data_dir, workspace, "1.0")

    runtime = await runtime_with(data_dir, "1.0.1")
    try:
        with pytest.raises(SessionPluginMismatchError) as info:
            await runtime.verify_session_plugins(session_id)
        assert "a.example==1.0" in str(info.value)
        assert "a.example==1.0.1" in str(info.value)
    finally:
        await runtime.dispose()


# --------------------------------------------------------------------------
# A missing key and an explicit null are different facts
# --------------------------------------------------------------------------


async def test_a_genuinely_missing_key_is_a_pre_v04_session(
    tmp_path: Path, workspace: Path
) -> None:
    """No key at all is what a session written before v0.4 looks like."""

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    # Written through SessionService, below the runtime, so the reserved key is
    # genuinely absent rather than filled in by `create_session`.
    session_id = await runtime.sessions.create_session(workspace, metadata={"cli": True})
    events = await runtime.sessions.read_session(session_id)
    assert "traceh_plugins" not in events[0].data["metadata"]

    await runtime.verify_session_plugins(session_id)
    result = await runtime.run_existing(session_id, "continue")
    assert result.reason == "completed"


async def test_an_explicit_null_is_malformed_not_a_pre_v04_session(
    tmp_path: Path, workspace: Path
) -> None:
    """`dict.get()` answers None for both cases; only one of them is benign.

    A recorded ``null`` is not something this runtime ever writes, so it is
    corrupt data. Treating it as "no plugins" would let a damaged session
    continue under a composition nobody recorded.
    """

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(
        workspace, metadata={"cli": True, "traceh_plugins": None}
    )
    events = await runtime.sessions.read_session(session_id)
    metadata = events[0].data["metadata"]
    assert "traceh_plugins" in metadata
    assert metadata["traceh_plugins"] is None

    with pytest.raises(SessionPluginMismatchError, match="malformed"):
        await runtime.verify_session_plugins(session_id)


async def test_an_explicit_null_also_blocks_running_the_session(
    tmp_path: Path, workspace: Path
) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(workspace, metadata={"traceh_plugins": None})

    with pytest.raises(SessionPluginMismatchError, match="malformed"):
        await runtime.run_existing(session_id, "continue")


async def test_an_explicit_null_is_rejected_on_a_plugin_runtime_too(
    tmp_path: Path, workspace: Path
) -> None:
    runtime = await runtime_with(tmp_path / "data", "2.0.0")
    try:
        session_id = await runtime.sessions.create_session(
            workspace, metadata={"traceh_plugins": None}
        )
        with pytest.raises(SessionPluginMismatchError, match="malformed"):
            await runtime.verify_session_plugins(session_id)
    finally:
        await runtime.dispose()


async def test_an_explicit_empty_list_is_a_valid_plugin_free_record(
    tmp_path: Path, workspace: Path
) -> None:
    """`[]` is what the runtime itself writes; it must stay acceptable."""

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(workspace, metadata={"traceh_plugins": []})

    await runtime.verify_session_plugins(session_id)


@pytest.mark.parametrize("bad", ["not-a-version", "", "1.0.0.x", "v1..2"])
async def test_invalid_recorded_version_is_still_rejected(
    tmp_path: Path, workspace: Path, bad: str
) -> None:
    """Equivalence must not become permissiveness: junk is still malformed."""

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(
        workspace, metadata={"traceh_plugins": [{"plugin_id": "a.example", "version": bad}]}
    )

    with pytest.raises(SessionPluginMismatchError, match="malformed"):
        await runtime.verify_session_plugins(session_id)


async def test_duplicate_recorded_ids_are_still_rejected(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(
        workspace,
        metadata={
            "traceh_plugins": [
                {"plugin_id": "a.example", "version": "1.0"},
                {"plugin_id": "a.example", "version": "1.0.0"},
            ]
        },
    )

    with pytest.raises(SessionPluginMismatchError, match="duplicate"):
        await runtime.verify_session_plugins(session_id)


# --------------------------------------------------------------------------
# The reserved metadata key
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        [],
        None,
        [{"plugin_id": "a.example", "version": "2.0.0"}],
        [{"plugin_id": "somebody.else", "version": "9.9.9"}],
        "not-even-a-list",
        {},
    ],
    ids=["empty-list", "none", "exact-match", "other-plugin", "string", "dict"],
)
async def test_reserved_key_is_rejected_on_presence_alone(
    tmp_path: Path, workspace: Path, supplied: object
) -> None:
    """Presence is the rule; the value is never consulted."""

    runtime = await runtime_with(tmp_path / "data", "2.0.0")
    try:
        with pytest.raises(ValueError, match="reserved"):
            await runtime.create_session(workspace, metadata={"traceh_plugins": supplied})
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("supplied", [[], None, []], ids=["empty", "none", "empty-again"])
async def test_reserved_key_is_rejected_on_a_plugin_free_runtime_too(
    tmp_path: Path, workspace: Path, supplied: object
) -> None:
    """With no plugins the expected value *is* `[]`; presence must still lose."""

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    with pytest.raises(ValueError, match="reserved"):
        await runtime.create_session(workspace, metadata={"traceh_plugins": supplied})


async def test_rejected_session_is_not_created(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    with pytest.raises(ValueError, match="reserved"):
        await runtime.create_session(workspace, metadata={"traceh_plugins": []})

    assert await runtime.sessions.list_sessions() == ()


async def test_other_metadata_is_still_stored(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.create_session(
        workspace,
        metadata={"cli": True, "operator": "alice", "nested": {"a": [1, 2]}},
    )

    events = await runtime.sessions.read_session(session_id)
    metadata = events[0].data["metadata"]
    assert metadata["cli"] is True
    assert metadata["operator"] == "alice"
    assert metadata["nested"] == {"a": [1, 2]}
    assert metadata["traceh_plugins"] == []


async def test_other_metadata_is_stored_alongside_real_plugin_identities(
    tmp_path: Path, workspace: Path
) -> None:
    runtime = await runtime_with(tmp_path / "data", "2.0.0")
    try:
        session_id = await runtime.create_session(workspace, metadata={"cli": True})
        events = await runtime.sessions.read_session(session_id)
        metadata = events[0].data["metadata"]
        assert metadata["cli"] is True
        assert metadata["traceh_plugins"] == [{"plugin_id": "a.example", "version": "2.0.0"}]
    finally:
        await runtime.dispose()


async def test_no_metadata_at_all_still_records_the_key(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.create_session(workspace)

    events = await runtime.sessions.read_session(session_id)
    assert events[0].data["metadata"] == {"traceh_plugins": []}
