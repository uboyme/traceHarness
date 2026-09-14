"""Delivery review observes real files and keeps scope decisions explicit."""

import asyncio
import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from promotion_fixtures import build_source_repository, make_bare_target
from test_product_adaptive import profile
from test_structured_product import StructuredProvider
from test_workspace_policy import _attached

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.api.workspaces import WorkspaceAccess
from traceh.artifacts.cas import LocalArtifactCas
from traceh.product.verification_review import REVIEW_GUIDANCE
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.workspaces.errors import WorkspaceStateError


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["diagnostic.py", "notes.txt"])
async def test_review_refreshes_actual_extra_files_and_replays(tmp_path, monkeypatch, extra):
    original = product._host_profile
    monkeypatch.setattr(
        product, "_host_profile", lambda mode: replace(original(mode), profile=profile(mode))
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()

    class Provider(StructuredProvider):
        packets = []

        async def complete(self, request):
            if REVIEW_GUIDANCE not in request.system_prompt:
                return await super().complete(request)
            packet = json.loads(request.system_prompt.split(REVIEW_GUIDANCE, 1)[1])
            self.packets.append(packet["delivery"])
            if len(self.packets) == 1:
                assert packet["delivery"]["changed_paths"] == ["added.txt"]
                return product._response(
                    "",
                    ToolCall(
                        "extra",
                        "apply_patch",
                        {
                            "path": extra,
                            "old_text": "",
                            "new_text": "extra",
                            "create": True,
                        },
                    ),
                )
            assert packet["delivery"]["changed_paths"] == sorted(["added.txt", extra])
            assert self.packets[0]["candidate_tree"] != self.packets[1]["candidate_tree"]
            return product._response("An extra file exists; no scope-compliance claim.")

    provider = Provider("delegate")
    await product._run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.MULTI,
        provider,
    )
    assert len(provider.packets) == 2
    sessions = SessionService(store)
    for sid in await sessions.list_sessions():
        assert not await verify_request_snapshots(sessions, SurfaceProjector(), sid)


@pytest.mark.asyncio
async def test_observation_rejects_wrong_owner_and_releases_lock_on_cancel(tmp_path):
    service, session_id, _ = await _attached(tmp_path, WorkspaceAccess.WRITABLE)
    record = (await service.catalog()).for_session(session_id)
    with pytest.raises(WorkspaceStateError):
        async with service.inspect_session(session_id, agent_id="wrong-owner"):
            pytest.fail("wrong owner entered observation")
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def observe():
        async with service.inspect_session(session_id, agent_id=record.agent_id):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    task = asyncio.create_task(observe())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    async with asyncio.timeout(2):
        async with service.inspect_session(session_id, agent_id=record.agent_id) as (handle, _):
            assert handle.session_id == session_id
