"""Real Product/Workspace/Supervisor path, deterministic model boundary."""

import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from collaboration_fixtures import PLAN
from promotion_fixtures import build_source_repository, make_bare_target
from test_product_adaptive import profile

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.cas import LocalArtifactCas
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.supervision.structured_collaboration import SUBMIT_COLLABORATION


class StructuredProvider:
    name = "product-provider"

    def __init__(self, action):
        self.action = action
        self.requests = []
        self.children = []

    async def complete(self, request):
        self.requests.append(request)
        names = {t.name for t in request.tools}
        if "traceh.product.investigation" in request.system_prompt:
            self.children.append(request)
            work = next(
                json.loads(m.content)
                for m in request.messages
                if m.role == "user" and m.content.startswith('{"briefing":')
            )
            assert work["format"] == 2
            assert work["main_work"] == PLAN["main_work"]
            assert all(
                work[k] == v
                for k, v in PLAN["children"][0].items()
                if k not in ("assignment_id", "role")
            )
            assert SUBMIT_COLLABORATION not in names
            if not any(m.role == "tool" for m in request.messages):
                return product._response(
                    "", ToolCall("child-read", "read_file", {"path": "tracked.txt"})
                )
            assert any("base" in m.content for m in request.messages if m.role == "tool")
            return product._response("Evidence: tracked.txt contains base.")
        if SUBMIT_COLLABORATION in names:
            assert {"read_file", "search_text", "list_files"} <= names
            return product._response(
                "",
                ToolCall(
                    "decision",
                    SUBMIT_COLLABORATION,
                    PLAN,
                ),
            )
        if "apply_patch" not in names:
            return product._response("Ready to decide.")
        assert not names & {
            SUBMIT_COLLABORATION,
            "delegate_investigation",
            "followup_investigation",
        }
        decision = next(
            json.loads(m.content)
            for m in request.messages
            if m.role == "tool" and m.name == SUBMIT_COLLABORATION
        )
        if self.action == "delegate":
            assert "tracked.txt contains base" in json.dumps(decision)
        if not any(m.role == "tool" and m.name == "apply_patch" for m in request.messages):
            return product._response(
                "",
                ToolCall(
                    "create-file",
                    "apply_patch",
                    {"path": "added.txt", "old_text": "", "new_text": "added\n", "create": True},
                ),
            )
        return product._response("Implemented using the returned evidence.")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delegate"])
async def test_structured_product_reaches_approval_and_replays(tmp_path, monkeypatch, action):
    original = product._host_profile
    monkeypatch.setattr(
        product, "_host_profile", lambda mode: replace(original(mode), profile=profile(mode))
    )
    source, revision = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    provider = StructuredProvider(action)
    try:
        await product._run_to_barrier(
            tmp_path,
            store,
            source,
            target,
            LocalArtifactCas(tmp_path / "cas"),
            RequestedTaskMode.MULTI,
            provider,
        )
    except AssertionError:
        for stream in await store.list_streams(prefix="session:"):
            for e in await SessionService(store).read_session(stream.removeprefix("session:")):
                if (
                    e.type in {"turn/error", "model/attempt-end", "tool/result"}
                    and e.data.get("status") != "succeeded"
                ):
                    print(e.type, e.data)
        raise
    assert bool(provider.children) == (action == "delegate")
    if action == "delegate":
        from traceh.agents import AgentDirectoryReader
        from traceh.evaluation.evaluators.product_handoffs import investigations

        child = next(
            r
            for r in (await AgentDirectoryReader(store).load()).records
            if r.session_id == provider.children[0].metadata["session_id"]
        )
        reports = await investigations(store, child.owner_agent_id, revision)
        assert len(reports) == 1
        assert reports[0]["parent_report_dispatches"]
    sessions = SessionService(store)
    for stream in await store.list_streams(prefix="session:"):
        sid = stream.removeprefix("session:")
        assert not await verify_request_snapshots(sessions, SurfaceProjector(), sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["missing-plan", "budget"])
async def test_multi_cannot_complete_without_required_child(tmp_path, monkeypatch, stop):
    from collaboration_fixtures import run_failed_product

    from traceh.agents import AgentDirectoryReader

    original = product._host_profile

    def limited(mode):
        configured = profile(mode)
        if stop == "budget":
            configured = replace(
                configured,
                coder=replace(
                    configured.coder, budget=replace(configured.coder.budget, max_steps=1)
                ),
            )
        return replace(original(mode), profile=configured)

    monkeypatch.setattr(product, "_host_profile", limited)
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()

    class Provider:
        name = "product-provider"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return product._response("The task is complete; no child is needed.")

    provider = Provider()
    _, events = await run_failed_product(tmp_path, store, source, target, provider)
    assert provider.calls == 1
    assert not any(
        e.type == "tool/result" and e.data.get("tool_name") == "apply_patch" for e in events
    )
    records = (await AgentDirectoryReader(store).load()).records
    assert not any(r.preset == "traceh.product.investigation" for r in records)
    assert any(e.type == "turn/end" for e in events)
