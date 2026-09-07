"""F2 host selection ownership, CAS, restart and failure evidence."""

import asyncio
from dataclasses import replace

import pytest
from retrieval_fixtures import build_case, select

from traceh.api.json_types import fingerprint
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.skill_selection import SkillSelectionWriteError


async def test_exact_operation_reentry_empty_selection_and_conflict(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        first = await select(runtime, session, values[0])
        assert await select(runtime, session, values[0]) == first
        with pytest.raises(ValueError, match="operation-conflict"):
            await select(runtime, session, values[1])
        with pytest.raises(ConcurrencyConflict):
            await select(runtime, session, values[1], operation="another")
        empty = await select(runtime, session, operation="clear", head=1)
        assert empty.seq == 2 and empty.data["skills"] == []
        assert len(await runtime.sessions.read_skill_selection(session)) == 2
        await runtime.run_existing(session, "boundary.notes")
        context = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert context.data["selection_head"]["head_seq"] == 2
        assert context.data["blocks"] == []
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_unknown_skill_rejected_before_write_and_selection_does_not_change_generation(
    tmp_path,
):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        async with runtime.loop.compositions.lease(
            workspace=tmp_path, session_id=session, turn_id="inspect", step_id="inspect"
        ) as before:
            generation = before.generation_id
            revision = before.snapshot.revision
            schemas = before.snapshot.tools
        invalid = replace(
            values[0], descriptor=replace(values[0].descriptor, skill_id="absent.notes")
        )
        with pytest.raises(ValueError, match="catalog-mismatch"):
            await select(runtime, session, invalid)
        assert await runtime.sessions.read_skill_selection(session) == ()
        await select(runtime, session, values[0])
        async with runtime.loop.compositions.lease(
            workspace=tmp_path, session_id=session, turn_id="inspect", step_id="inspect"
        ) as after:
            assert (generation, revision, schemas) == (
                after.generation_id,
                after.snapshot.revision,
                after.snapshot.tools,
            )
        events = await runtime.sessions.read_session(session)
        assert not any(e.type == "composition/migration-authorized" for e in events)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("commit", [False, True])
async def test_selection_failed_append_has_exact_commit_evidence(tmp_path, monkeypatch, commit):
    runtime, store, _, session, values = await build_case(tmp_path)
    original = store.append

    async def append(stream, **kwargs):
        if stream.startswith("context-selection:"):
            if commit:
                await original(stream, **kwargs)
            raise OSError("injected storage failure")
        return await original(stream, **kwargs)

    monkeypatch.setattr(store, "append", append)
    try:
        with pytest.raises(SkillSelectionWriteError) as caught:
            await select(runtime, session, values[0])
        assert caught.value.committed is commit
        assert len(await runtime.sessions.read_skill_selection(session)) == int(commit)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_repeated_cancel_holds_lease_until_selection_append_converges(tmp_path, monkeypatch):
    runtime, store, _, session, values = await build_case(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.append

    async def append(stream, **kwargs):
        if stream.startswith("context-selection:"):
            entered.set()
            await release.wait()
        return await original(stream, **kwargs)

    monkeypatch.setattr(store, "append", append)
    task = asyncio.create_task(select(runtime, session, values[0]))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_skill_selection(session)
        assert len(events) == 1
        assert events[0].data["catalog_digest"] == fingerprint(
            [v.descriptor.to_dict() for v in values]
        )
    finally:
        release.set()
        await runtime.dispose()
        await store.aclose()
