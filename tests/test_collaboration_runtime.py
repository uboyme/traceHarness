"""Bounded collaboration through the real loop and child Supervisor."""

import asyncio
import json

import pytest
from supervision_fixtures import SPEC, GatedProvider, RuntimeFactory
from test_investigation_tools import WORK, BoundPolicy
from test_product_f3_e2e import _response

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.llm import ToolCall
from traceh.product.collaboration import (
    CollaborationContinuation,
    CollaborationDispatchFailed,
    CollaborationExecutionStopped,
    CollaborationPlanInvalid,
    CollaborationPolicy,
)
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import AgentRunReportReader, ProcessAgentSupervisor
from traceh.supervision.delegation import InvestigationToolset
from traceh.supervision.structured_collaboration import (
    SUBMIT_COLLABORATION,
    CollaborationPlanTool,
    investigator_role,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "local",
        "delegate",
        "invalid",
        "duplicate",
        "cancel",
        "timeout",
        "scout-denied",
        "text",
        "budget",
        "review-cancel",
        "review-budget",
    ],
)
async def test_decision_loop_and_owned_child_convergence(tmp_path, monkeypatch, mode):
    import traceh.supervision.structured_collaboration as guidance

    marker = "Explicit test navigation: inspect provenance before allocating."
    monkeypatch.setattr(guidance, "ALLOCATION_GUIDANCE", marker)
    store = InMemoryEventStore()
    gate = GatedProvider()
    review_entered = asyncio.Event()
    factory = RuntimeFactory(store, tmp_path, provider=gate)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )

    class Provider:
        name = "decision-test"

        def __init__(self):
            self.calls = 0
            self.report = None

        async def complete(self, request):
            self.calls += 1
            from traceh.product.verification_review import REVIEW_GUIDANCE

            if REVIEW_GUIDANCE in request.system_prompt and mode == "review-cancel":
                review_entered.set()
                await asyncio.Event().wait()
            names = {t.name for t in request.tools}
            if mode == "budget":
                return _response("", ToolCall("inspect", "list_files", {"path": "."}))
            if mode == "scout-denied" and self.calls <= 2:
                return _response(
                    "Narrative must not reach decision",
                    ToolCall(
                        f"forbidden-{self.calls}",
                        "apply_patch",
                        {"path": "bad.txt", "old_text": "", "new_text": "bad", "create": True},
                    ),
                )
            if SUBMIT_COLLABORATION in names:
                assert marker in request.system_prompt
                assert guidance.PLAN_REQUIREMENT in request.system_prompt
                assert marker in next(
                    t.description for t in request.tools if t.name == SUBMIT_COLLABORATION
                )
                if mode == "text":
                    return _response("I prefer to complete this alone.")
                args = {
                    "main_work": WORK["main_work"],
                    "children": [
                        {
                            "assignment_id": "explicit-test-investigation",
                            "role": "investigator",
                            **{k: v for k, v in WORK.items() if k != "main_work"},
                        }
                    ],
                }
                if mode == "invalid":
                    args["children"] = None
                if mode == "local":
                    args["action"] = "local"
                calls = [ToolCall(f"decide-{self.calls}", SUBMIT_COLLABORATION, args)]
                if mode == "duplicate":
                    calls.append(ToolCall(f"another-{self.calls}", SUBMIT_COLLABORATION, args))
                return _response("", *calls)
            if self.calls == 1:
                return _response("Ready to decide.")
            self.report = next(
                json.loads(m.content)
                for m in request.messages
                if m.role == "tool" and m.name == SUBMIT_COLLABORATION
            )
            return _response("Finished using available evidence.")

    provider = Provider()
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main",
            provider=provider.name,
            model="model",
            max_steps=1 if mode == "budget" else (3 if mode == "review-budget" else 12),
            tool_timeout_seconds=1 if mode == "timeout" else 60,
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        additional_tools=(*toolset.tools, CollaborationPlanTool((investigator_role(
            toolset.control
        ),))),
    )
    running = asyncio.create_task(runtime.run_existing(owner.session_id, "Investigate the target"))
    try:
        if mode in {
            "delegate",
            "cancel",
            "timeout",
            "scout-denied",
            "review-cancel",
            "review-budget",
        }:
            await asyncio.wait_for(gate.entered.wait(), 5)
            assert not running.done()
            if mode == "cancel":
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            elif mode == "timeout":
                with pytest.raises(CollaborationDispatchFailed):
                    await asyncio.wait_for(running, 5)
            else:
                gate.release.set()
                if mode == "review-cancel":
                    await asyncio.wait_for(review_entered.wait(), 5)
                    running.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await running
                elif mode == "review-budget":
                    with pytest.raises(CollaborationExecutionStopped, match="max_steps_exceeded"):
                        await running
                    assert provider.calls == 3
                else:
                    await running
        else:
            with pytest.raises(
                CollaborationPlanInvalid if mode == "text" else CollaborationExecutionStopped
            ):
                await running
        children = [
            r
            for r in (await AgentDirectoryReader(store).load()).records
            if r.owner_agent_id == owner.agent_id
        ]
        assert len(children) == (
            1
            if mode
            in {"delegate", "cancel", "timeout", "scout-denied", "review-cancel", "review-budget"}
            else 0
        )
        if children:
            child = children[0]
            accepted = next(iter(await AgentInboxReader(store).load(child.agent_id)))
            report = await AgentRunReportReader(store).load(
                child.agent_id, accepted.message.message_id
            )
            assert report.status == ("cancelled" if mode in {"cancel", "timeout"} else "completed")
        if mode == "delegate":
            assert provider.report["children"][0]["statement"] == "answer 0"
        if mode in {"invalid", "duplicate", "local", "text"}:
            assert provider.calls == (1 if mode == "text" else 12)
            assert provider.report is None
        if mode == "scout-denied":
            assert provider.calls == 5
            assert not (tmp_path / "bad.txt").exists()
        assert not await runtime.check_invariants(owner.session_id)
    finally:
        gate.release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.dispose()
        await supervisor.aclose()
