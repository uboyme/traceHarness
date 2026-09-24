"""DA Product path: real Git workspaces, Tool calls, child tree and verifier."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product_test
from collaboration_fixtures import PLAN
from promotion_fixtures import build_source_repository, make_bare_target

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.capture import PatchCaptureService
from traceh.artifacts.cas import LocalArtifactCas
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import AgentRunReportReader
from traceh.tui.task_conversation import TaskConversationReader

pytestmark = pytest.mark.asyncio


def profile(mode):
    base = product_test._profile(mode)
    return replace(
        base,
        coder=replace(
            base.coder,
            budget=product_test._limits(
                max_tokens=80000,
                max_steps=30,
                max_tool_calls=30,
                max_wall_milliseconds=300000,
                max_children=2,
                max_depth=1,
                max_processes=2,
            ),
            max_turn_wall_milliseconds=60000,
        ),
        investigator=replace(
            base.investigator,
            budget=product_test._limits(
                max_tokens=5000,
                max_steps=4,
                max_tool_calls=4,
                max_wall_milliseconds=30000,
                max_children=0,
                max_depth=0,
                max_processes=0,
            ),
            max_turn_wall_milliseconds=30000,
        ),
        retained_tokens=2000,
        task_budget=replace(base.task_budget, max_depth=2),
    )


class DelegatingProvider:
    name = "product-provider"

    def __init__(self, child_failure):
        self.child_failure = child_failure
        self.main_requests = []
        self.child_requests = []

    async def complete(self, request):
        names = {tool.name for tool in request.tools}
        if "traceh.product.investigation" in request.system_prompt:
            self.child_requests.append(request)
            assert "submit_collaboration_plan" not in names
            if self.child_failure:
                raise RuntimeError("explicit child model failure")
            if not any(m.role == "tool" for m in request.messages):
                return product_test._response(
                    "", ToolCall("read", "read_file", {"path": "tracked.txt"})
                )
            return product_test._response("tracked.txt contains base; original source inspected.")
        self.main_requests.append(request)
        if "submit_collaboration_plan" in names:
            return product_test._response(
                "",
                ToolCall(
                    "decision",
                    "submit_collaboration_plan",
                    PLAN,
                ),
            )
        if "apply_patch" not in names:
            return product_test._response("Ready for decision.")
        reports = next(
            json.loads(m.content)["children"]
            for m in request.messages
            if m.role == "tool" and m.name == "submit_collaboration_plan"
        )
        assert len(reports) == 1
        report = reports[0]
        assert report["status"] == ("failed" if self.child_failure else "completed")
        if not self.child_failure:
            assert "contains base" in report["statement"]
        return await product_test._ProductProvider().complete(request)


@pytest.mark.parametrize("child_failure", [False, True])
async def test_multi_collects_or_converges_children_before_capture(
    tmp_path, monkeypatch, child_failure
):
    original_profile = product_test._host_profile
    monkeypatch.setattr(
        product_test,
        "_host_profile",
        lambda mode: replace(original_profile(mode), profile=profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    provider = DelegatingProvider(child_failure)
    original_capture = PatchCaptureService.capture
    captures = []

    async def checked_capture(capture, agent_id, message_id):
        captures.append(agent_id)
        return await original_capture(capture, agent_id, message_id)

    monkeypatch.setattr(PatchCaptureService, "capture", checked_capture)
    if child_failure:
        from collaboration_fixtures import run_failed_product

        _, events = await run_failed_product(tmp_path, store, source, target, provider)
        assert not captures
        assert any(
            e.type == "runtime/error" and e.data["error_type"] == "CollaborationChildIncomplete"
            for e in events
        )
        assert provider.child_requests and len(provider.main_requests) == 1
        return
    task_id, _, _ = await product_test._run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.MULTI,
        provider,
    )
    assert len(captures) == 1
    records = (await AgentDirectoryReader(store).load()).records
    main = next(record for record in records if record.agent_id == captures[0])
    children = [record for record in records if record.owner_agent_id == main.agent_id]
    assert len(children) == 1
    for child in children:
        accepted = next(iter(await AgentInboxReader(store).load(child.agent_id)))
        report = await AgentRunReportReader(store).load(child.agent_id, accepted.message.message_id)
        assert report.status == ("failed" if child_failure else "completed")
    assert provider.child_requests and provider.main_requests
    reopened = await product_test._build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        product_test.ProductTurnActions(),
        RequestedTaskMode.MULTI,
        provider,
        line_adapter=False,
    )
    try:
        observation = await reopened.observation.load(task_id)
        conversation = await TaskConversationReader(store).load(observation)
        assert len(conversation.roles) == 2
        visible_children = [role for role in conversation.roles if role.role == "investigator"]
        assert {role.session_id for role in visible_children} == {
            child.session_id for child in children
        }
        assert all("调查目标：" in role.messages[0][1] for role in visible_children)
        from datetime import UTC, datetime, timedelta

        from traceh.api.json_types import fingerprint
        from traceh.evolution.background import (
            BackgroundOptimizationHost,
            BackgroundPeriod,
            EpisodeReservation,
            EpisodeSettlement,
        )
        from traceh.session.service import SessionService

        sessions = SessionService(store)
        workspace = await sessions.workspace_for(observation.summary.confirmation_session_id)
        received = []

        async def execute(identity, observations, seen, deadline):
            received.extend(observations)
            return EpisodeSettlement("explicit-product-test-evidence", None, True, True)

        background = BackgroundOptimizationHost(
            sessions,
            BackgroundPeriod(
                "product-test-period",
                str(workspace.resolve()),
                fingerprint("source"),
                fingerprint("plan"),
                datetime.now(UTC) + timedelta(hours=1),
                1,
                1000,
                10,
                1,
            ),
            reservation=EpisodeReservation(1000),
            execute=execute,
        )
        try:
            await background.foreground(True)
            await background.set_enabled(True)
            # A clean collaborative task shows no mechanism, so nothing is recorded.
            assert not await background.observe_product(reopened.observation, task_id)
            await background.foreground(False)
            await background.wait_idle()
            assert not received
            from types import SimpleNamespace

            with pytest.raises(ValueError, match="store-mismatch"):
                await background.observe_product(
                    SimpleNamespace(store=InMemoryEventStore()), task_id
                )
            background.period = replace(
                background.period, workspace=str((tmp_path / "elsewhere").resolve())
            )
            from traceh.evolution.product_feedback import product_findings

            with pytest.raises(ValueError, match="outside-scope"):
                await product_findings(sessions, background.period, reopened.observation, task_id)
        finally:
            await background.aclose()
        # An old observation cannot silently adopt a child stream it never saw.
        stale = replace(
            observation,
            stream_heads=tuple(
                head
                for head in observation.stream_heads
                if head.stream_id != "session:" + children[0].session_id
            ),
        )
        from traceh.product.errors import ProductStateError

        with pytest.raises(ProductStateError) as refused:
            await TaskConversationReader(store).load(stale)
        assert refused.value.code == "product-conversation-session-unbound"
    finally:
        await reopened.aclose()
