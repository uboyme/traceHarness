"""Same-Store FTS schema, derived data, transactional rebuild and close ownership."""

import asyncio
import sqlite3
import threading

import pytest
from retrieval_fixtures import build_case, select

from traceh.session import context_index
from traceh.session.retrieval import tokenize
from traceh.session.skill_retrieval import prepare_corpus
from traceh.session.sqlite import ContextIndexWriteError, EventStoreSchemaError, SqliteEventStore


async def corpus_for(runtime, session, workspace):
    async with runtime.loop.compositions.lease(
        workspace=workspace, session_id=session, turn_id="inspect", step_id="inspect"
    ) as active:
        selections = await runtime.sessions.read_skill_selection(session)
        return prepare_corpus(
            active.snapshot, selections, session, runtime.config.context_input.skills
        )[0]


async def test_schema_two_backup_restore_and_index_rebuild_preserve_events(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        corpus = await corpus_for(runtime, session, tmp_path)
        assert await store.query_context_index(corpus, tokenize("边界")) is None
        before = await store.read(f"session:{session}")
        await runtime.skill_context.rebuild_index(session)
        hits = await store.query_context_index(corpus, tokenize("边界"))
        assert len(hits) == 1
        assert await store.read(f"session:{session}") == before
        await runtime.skill_context.rebuild_index(session)
        assert await store.query_context_index(corpus, tokenize("边界")) == hits
        receipt = await store.backup(tmp_path / "backup")
        assert receipt.schema_version == 2
        await SqliteEventStore.restore(tmp_path / "backup", tmp_path / "restore")
        restored = SqliteEventStore(tmp_path / "restore")
        try:
            assert await restored.query_context_index(corpus, tokenize("边界")) == hits
            assert await restored.read(f"session:{session}") == before
        finally:
            await restored.aclose()
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("damage", ["manifest", "items", "fts"])
async def test_known_derived_row_damage_is_unavailable_and_explicitly_rebuildable(tmp_path, damage):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        corpus = await corpus_for(runtime, session, tmp_path)
        await runtime.skill_context.rebuild_index(session)
        tables = {
            "manifest": "context_index_manifest",
            "items": "context_index_items",
            "fts": "context_fts",
        }
        with sqlite3.connect(store.path) as connection:
            connection.execute(f"DELETE FROM {tables[damage]}")
        assert await store.query_context_index(corpus, tokenize("边界")) is None
        await runtime.skill_context.rebuild_index(session)
        assert len(await store.query_context_index(corpus, tokenize("边界"))) == 1
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_stale_source_head_rejects_index_rebuild_without_half_publication(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        corpus = await corpus_for(runtime, session, tmp_path)
        await select(runtime, session, values[1], operation="changed", head=1)
        with pytest.raises(ContextIndexWriteError) as failed:
            await store.rebuild_context_index(corpus)
        assert failed.value.published is False
        assert "source-changed" in str(failed.value.__cause__)
        assert await store.query_context_index(corpus, tokenize("边界")) is None
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT count(*) FROM context_index_manifest").fetchone() == (
                0,
            )
            assert connection.execute("SELECT count(*) FROM context_index_items").fetchone() == (0,)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_partial_rebuild_failure_rolls_back_previous_complete_index(tmp_path, monkeypatch):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        corpus = await corpus_for(runtime, session, tmp_path)
        hits = await store.query_context_index(corpus, tokenize("边界"))

        def fail(connection, corpus):
            connection.execute("DELETE FROM context_index_items WHERE corpus_key=?", (corpus.key,))
            raise RuntimeError("injected mid-rebuild failure")

        monkeypatch.setattr(context_index, "rebuild", fail)
        with pytest.raises(ContextIndexWriteError) as failed:
            await runtime.skill_context.rebuild_index(session)
        assert failed.value.published is True
        assert "mid-rebuild" in str(failed.value.__cause__)
        assert await store.query_context_index(corpus, tokenize("边界")) == hits
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("probe_available", [True, False])
async def test_after_commit_rebuild_failure_reports_publication_without_retry(
    tmp_path, monkeypatch, probe_available
):
    runtime, store, _, session, values = await build_case(tmp_path)
    calls = []
    original = store._context_index_sync

    def fail(corpus, *, terms=None, probe=False):
        calls.append(probe)
        if probe:
            if not probe_available:
                raise RuntimeError("probe unavailable")
            return original(corpus, probe=True)
        original(corpus)
        raise RuntimeError("failed after commit")

    try:
        await select(runtime, session, values[0])
        corpus = await corpus_for(runtime, session, tmp_path)
        monkeypatch.setattr(store, "_context_index_sync", fail)
        with pytest.raises(ContextIndexWriteError) as error:
            await runtime.skill_context.rebuild_index(session)
        assert error.value.published is (True if probe_available else None)
        assert "after commit" in str(error.value.__cause__)
        assert calls == [False, True]
        monkeypatch.setattr(store, "_context_index_sync", original)
        assert len(await store.query_context_index(corpus, tokenize("边界"))) == 1
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_cancel_and_store_close_converge_running_index_worker(tmp_path, monkeypatch):
    runtime, store, _, session, values = await build_case(tmp_path)
    await select(runtime, session, values[0])
    entered, released = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = context_index.rebuild

    def gated(connection, corpus):
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10), "test gate timed out"
        original(connection, corpus)

    monkeypatch.setattr(context_index, "rebuild", gated)
    task = asyncio.create_task(runtime.skill_context.rebuild_index(session))
    closing = None
    try:
        await entered.wait()
        task.cancel()
        closing = asyncio.create_task(store.aclose())
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and not closing.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        reopened = SqliteEventStore(tmp_path / "events")
        try:
            with sqlite3.connect(reopened.path) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM context_index_manifest"
                ).fetchone() == (1,)
        finally:
            await reopened.aclose()
    finally:
        released.set()
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("damage", ["old-version", "missing-shadow", "unknown-object"])
async def test_open_rejects_old_or_foreign_schema_without_mutation(tmp_path, damage):
    store = SqliteEventStore(tmp_path / "events")
    await store.aclose()
    with sqlite3.connect(store.path) as connection:
        if damage == "old-version":
            connection.execute("PRAGMA user_version=1")
        elif damage == "missing-shadow":
            connection.execute("DROP TABLE context_fts_idx")
        else:
            connection.execute("CREATE TABLE context_surprise(value TEXT)")
    before = store.path.read_bytes()
    with pytest.raises(EventStoreSchemaError):
        SqliteEventStore(tmp_path / "events")
    assert store.path.read_bytes() == before
