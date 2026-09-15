"""Work-shaped delegation reuses real Supervisor delivery and reports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from collaboration_fixtures import MAIN_WORK
from supervision_fixtures import SPEC, GatedProvider, RuntimeFactory

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.agents import AgentSpec
from traceh.api.tools import ToolExecutionContext
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import AgentToolBindingError, ProcessAgentSupervisor
from traceh.supervision.delegation import InvestigationBinding, InvestigationToolset

pytestmark = pytest.mark.asyncio


class BoundPolicy:
    def __init__(self, owner_id):
        self.owner_id = owner_id
        self.revision = "1" * 40

    def prepare(self, owner):
        if owner.agent_id != self.owner_id:
            raise AgentToolBindingError("unbound parent")
        return InvestigationBinding(
            AgentSpec(preset="readonly", workspace_id="source", owner_agent_id=owner.agent_id),
            "source",
            self.revision,
        )

    async def validate_child(self, owner, child):
        binding = self.prepare(owner)
        if child.preset != binding.spec.preset or child.owner_agent_id != owner.agent_id:
            raise AgentToolBindingError("unbound child")
        return binding


def context(tmp_path, session_id, call="first"):
    return ToolExecutionContext(session_id, "turn", "step", call, tmp_path, tmp_path)


WORK = {
    "scope": "Inspect the original failure boundary",
    "exclusions": "No implementation or unrelated work",
    "main_work": MAIN_WORK,
    "goal": "Find the failure boundary",
    "deliverable": "Source locations and explanation",
    "briefing": "Inspect the original source; do not assume a proposed patch is correct.",
}


async def test_two_children_run_concurrently_and_reports_bind_exact_message(tmp_path):
    store = InMemoryEventStore()
    gates = [GatedProvider(), GatedProvider()]

    class Factory(RuntimeFactory):
        def _runtime(self):
            if self.provisions > 1:
                self.provider = gates[self.provisions - 2]
            return super()._runtime()

    factory = Factory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    policy = BoundPolicy(owner.agent_id)
    tools = {
        t.name: t
        for t in InvestigationToolset(
            supervisor=supervisor, owner_agent_id=owner.agent_id, event_store=store, policy=policy
        ).tools
    }
    ctx = context(tmp_path, owner.session_id)
    try:
        first = await tools["delegate_investigation"].execute(WORK, ctx)
        repeated = await tools["delegate_investigation"].execute(WORK, ctx)
        assert first.data == repeated.data
        second = await tools["delegate_investigation"].execute(
            WORK, replace(ctx, tool_call_id="second")
        )
        assert json.loads(second.content)["agent_id"] != json.loads(first.content)["agent_id"]
        await asyncio.wait_for(asyncio.gather(*(g.entered.wait() for g in gates)), 1)
        assert len((await AgentDirectoryReader(store).load()).records) == 3
        pending = await tools["collect_investigation"].execute(
            {**first.data, "wait_seconds": 0}, ctx
        )
        assert pending.data["status"] == "pending"
        for gate in gates:
            gate.release.set()
        report = await tools["collect_investigation"].execute(
            {**first.data, "wait_seconds": 1}, ctx
        )
        assert report.data["status"] == "completed"
        assert json.loads(report.content)["statement"] == "answer 0"
        assert report.evidence
        inbox = await AgentInboxReader(store).load(first.data["agent_id"])
        assert len(tuple(inbox)) == 1
        policy.revision = "2" * 40
        with pytest.raises(AgentToolBindingError):
            await tools["collect_investigation"].execute({**first.data, "wait_seconds": 0}, ctx)
    finally:
        for gate in gates:
            gate.release.set()
        await supervisor.aclose()


async def test_wrong_session_cannot_create_or_collect(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    tools = {
        t.name: t
        for t in InvestigationToolset(
            supervisor=supervisor,
            owner_agent_id=owner.agent_id,
            event_store=store,
            policy=BoundPolicy(owner.agent_id),
        ).tools
    }
    try:
        with pytest.raises(AgentToolBindingError):
            await tools["delegate_investigation"].execute(WORK, context(tmp_path, "foreign"))
        assert factory.provisions == 1
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("field", ["scope", "exclusions", "main_work"])
async def test_incomplete_work_cannot_create_an_agent(tmp_path, field):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    tools = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )
    try:
        arguments = {k: v for k, v in WORK.items() if k != field}
        with pytest.raises(ValueError):
            await tools.tools[0].execute(arguments, context(tmp_path, owner.session_id))
        assert len((await AgentDirectoryReader(store).load()).records) == 1
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_send_cancel_waits_for_created_child_cleanup(tmp_path, cleanup_fails):
    entered, release = asyncio.Event(), asyncio.Event()

    class SendGated(ProcessAgentSupervisor):
        async def send(self, *args, **kwargs):
            result = await super().send(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

    store = InMemoryEventStore()
    provider = GatedProvider()

    class FailingCleanup:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def dispose(self):
            await self.inner.dispose()
            raise RuntimeError("fixture investigation cleanup failed")

    class Factory(RuntimeFactory):
        async def provision(self, spec, **kwargs):
            result = await super().provision(spec, **kwargs)
            return FailingCleanup(result) if cleanup_fails and spec.owner_agent_id else result

    factory = Factory(store, tmp_path, provider=provider)
    supervisor = SendGated(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    delegate = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    ).tools[0]
    task = asyncio.create_task(delegate.execute(WORK, context(tmp_path, owner.session_id)))
    try:
        await entered.wait()
        await provider.entered.wait()
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        if cleanup_fails:
            from test_agent_tools import failure_leaves

            assert any(
                str(error) == "fixture investigation cleanup failed"
                for error in failure_leaves(cancelled.value.__cause__)
            )
        # The accepted message retains its cancelled report after the Tool exits.
        records = (await AgentDirectoryReader(store).load()).records
        child = next(r for r in records if r.owner_agent_id == owner.agent_id)
        accepted = next(iter(await AgentInboxReader(store).load(child.agent_id)))
        report = await supervisor.report(child.agent_id, accepted.message.message_id)
        assert report.status == "cancelled"
    finally:
        release.set()
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if cleanup_fails:
            with pytest.raises(BaseExceptionGroup) as closing:
                await supervisor.aclose()
            from test_agent_tools import failure_leaves

            assert any(
                str(error) == "fixture investigation cleanup failed"
                for error in failure_leaves(closing.value)
            )
        else:
            await supervisor.aclose()
