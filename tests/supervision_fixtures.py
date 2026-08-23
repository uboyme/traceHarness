"""Shared scaffolding for the Stage C supervision tests.

Not named ``test_*`` so pytest does not collect it, and imported as a top-level
module because ``tests/`` is not a package - the same convention the plugin
fixtures follow.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from traceh.agents import AgentInboxReader, AgentRegistrar
from traceh.api.agents import AgentMessage, AgentRecord, AgentSpec, MessageTarget
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import Durability, EventStore, InMemoryEventStore
from traceh.supervision import (
    AgentDeliveryReader,
    AgentRuntimeExecution,
    ChildProvisioningProposal,
    agent_delivery_stream,
)

SPEC = AgentSpec(preset="unit-preset", workspace_id="unit-workspace")


class RequestedChildPolicy:
    """An explicit test-host policy that accepts the requested identifiers."""

    def propose_child(
        self,
        *,
        owner: AgentRecord,
        requested_preset: str,
        requested_workspace_id: str,
    ) -> ChildProvisioningProposal:
        del owner
        return ChildProvisioningProposal(
            preset=requested_preset,
            workspace_id=requested_workspace_id,
        )


CHILD_POLICY = RequestedChildPolicy()


def message(message_id: str = "m1", **overrides) -> AgentMessage:
    fields = {
        "message_id": message_id,
        "content": "do the thing",
        "source": "operator",
        **overrides,
    }
    return AgentMessage(**fields)


def scripted(count: int = 32) -> ScriptedLlmProvider:
    """A provider that answers without ever asking for a tool."""

    return ScriptedLlmProvider(tuple(ModelResponse(content=f"answer {i}") for i in range(count)))


class RecordingProvider:
    """Counts calls, so a test can prove a model was *not* reached."""

    def __init__(self, inner=None) -> None:
        self.inner = inner if inner is not None else scripted()
        self.name = self.inner.name
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return await self.inner.complete(request)


class GatedProvider:
    """Blocks inside the model call until released.

    Lets a test hold a Turn open at an exact point - which is what makes
    "interrupt while the model is running" a deterministic scenario rather than
    a race against a timer.
    """

    def __init__(self, inner=None) -> None:
        self.inner = inner if inner is not None else scripted()
        self.name = self.inner.name
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await self.inner.complete(request)


class RuntimeFactory:
    """Builds one exclusive `AgentRuntime` per Agent, on one shared store.

    Deliberately written by the test rather than shipped by the library:
    mapping a ``preset`` to a model or a ``workspace_id`` to a directory is a
    host decision, and the control plane refuses to guess it.
    """

    def __init__(self, store: EventStore, root: Path, *, provider=None) -> None:
        self.store = store
        self.root = root
        self.provider = provider
        self.provisions = 0
        self.activations = 0
        self.executions: list[AgentRuntimeExecution] = []
        self.provision_error: BaseException | None = None
        self.activate_error: BaseException | None = None
        self.provision_gate: asyncio.Event | None = None
        self.activate_gate: asyncio.Event | None = None
        self.provision_entered = asyncio.Event()
        self.activate_entered = asyncio.Event()

    def _runtime(self):
        return build_default_runtime(
            RuntimeConfig(data_dir=self.root / "data", provider="scripted", model="unit-model"),
            provider=self.provider if self.provider is not None else scripted(),
            event_store=self.store,
        )

    def _workspace(self, spec: AgentSpec) -> Path:
        workspace = self.root / spec.workspace_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def provision(self, spec, *, agent_id, session_id):
        self.provision_entered.set()
        if self.provision_gate is not None:
            await self.provision_gate.wait()
        if self.provision_error is not None:
            raise self.provision_error
        self.provisions += 1
        runtime = self._runtime()
        workspace = self._workspace(spec)
        created = (
            await runtime.create_session(workspace, session_id=session_id)
            if session_id is not None
            else await runtime.create_session(workspace)
        )
        execution = AgentRuntimeExecution(runtime, created)
        self.executions.append(execution)
        return execution

    async def activate(self, record: AgentRecord):
        self.activate_entered.set()
        if self.activate_gate is not None:
            await self.activate_gate.wait()
        if self.activate_error is not None:
            raise self.activate_error
        self.activations += 1
        execution = AgentRuntimeExecution(self._runtime(), record.session_id)
        self.executions.append(execution)
        return execution

    @property
    def calls(self) -> int:
        return self.provisions + self.activations


class StubExecution:
    """An `AgentExecution` with no runtime behind it.

    Used where the test is about the Supervisor's own state machine and a real
    Turn would only add noise.
    """

    def __init__(self, store: EventStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self.disposals = 0
        self.turns = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_store(self) -> EventStore:
        return self._store

    async def run_turn(self, turn_input):  # pragma: no cover - overridden per test
        raise NotImplementedError

    async def cancel_turn(self, *, reason: str) -> bool:
        return False

    async def dispose(self) -> None:
        self.disposals += 1


async def register_agent(
    store: EventStore,
    *,
    agent_id: str = "agent-1",
    session_id: str = "session-1",
    request_id: str = "request-1",
) -> AgentRecord:
    """Create a durable Agent identity without a Supervisor."""

    return await AgentRegistrar(store).create_agent(
        SPEC, request_id=request_id, agent_id=agent_id, session_id=session_id
    )


async def read_delivery(store: EventStore, agent_id: str):
    """Rebuild the delivery log through objects that never saw the write path."""

    inbox = await AgentInboxReader(store).load(agent_id)
    return await AgentDeliveryReader(store).load(agent_id, inbox)


async def raw_delivery_append(
    store: EventStore,
    agent_id: str,
    data: dict,
    *,
    event_type: str,
    schema_version: int = 1,
    stream_id: str | None = None,
) -> None:
    """Append a delivery event directly, bypassing every service check."""

    stream = stream_id if stream_id is not None else agent_delivery_stream(agent_id)
    head = await store.head(stream)
    await store.append(
        stream,
        expected_seq=head,
        events=(PendingEvent(type=event_type, data=data, schema_version=schema_version),),
        durability=Durability.SYNC,
    )


def delivery_envelope(
    data: dict,
    *,
    event_type: str,
    agent_id: str = "agent-1",
    seq: int = 1,
    **overrides,
) -> EventEnvelope:
    """Build a delivery envelope directly, bypassing the store's encoding."""

    fields = {
        "event_id": uuid4(),
        "stream_id": agent_delivery_stream(agent_id),
        "seq": seq,
        "type": event_type,
        "schema_version": 1,
        "data": data,
        "occurred_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return EventEnvelope(**fields)


def replace_envelope_field(event: EventEnvelope, field: str, value) -> EventEnvelope:
    fields = {name: getattr(event, name) for name in event.__slots__}
    fields[field] = value
    return EventEnvelope(**fields)


async def settle(times: int = 5) -> None:
    """Yield to the event loop a bounded number of times.

    This is not a timing guess and never stands in for a real signal. It is
    used only where the assertion that follows is *negative* - "the worker was
    given every chance to run and still wrote nothing" - or to let a Task that
    was just created reach its first await. Anything that must actually happen
    is awaited through an `asyncio.Event`, a gate or a real append latch.
    """

    for _ in range(times):
        await asyncio.sleep(0)


def never_retrieved(reports: list[dict]) -> list[dict]:
    return [item for item in reports if "never retrieved" in str(item.get("message", ""))]


def destroyed_pending(reports: list[dict]) -> list[dict]:
    return [item for item in reports if "was destroyed" in str(item.get("message", ""))]


__all__ = [
    "CHILD_POLICY",
    "GatedProvider",
    "InMemoryEventStore",
    "MessageTarget",
    "RecordingProvider",
    "RuntimeFactory",
    "SPEC",
    "StubExecution",
    "delivery_envelope",
    "destroyed_pending",
    "message",
    "never_retrieved",
    "raw_delivery_append",
    "read_delivery",
    "register_agent",
    "replace_envelope_field",
    "scripted",
    "settle",
]
