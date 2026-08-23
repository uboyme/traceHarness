"""The narrow seam between a Supervisor and whatever actually runs a Turn.

The Supervisor must not reach into `AgentRuntime`'s internals - not its active
Turn table, not its locks, not its plugin coordinator. It needs exactly four
things, and this protocol is all four:

* run one message on this Agent's Session;
* cancel whatever Turn is running there;
* dispose the runtime it exclusively owns;
* expose the Session and `EventStore` identity so the Supervisor can *prove*
  the Turn will append where the claim says it will.

That last one is a validation surface, not a convenience. Two stores can be
configured identically and still be two different logs, so the Supervisor
compares object identity: a Turn that wrote to the wrong store would leave a
durable claim pointing at a Session history that does not contain it.

`AgentRuntime` and `AgentLoop` do not know this module exists. The dependency
points one way only.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from traceh.api.agents import AgentRecord, AgentSpec
from traceh.api.turns import TurnInput
from traceh.concurrency import await_worker_convergence
from traceh.runtime.agent_loop import TurnResult
from traceh.session.event_feed import PublishingEventStore
from traceh.session.event_store import EventStore


def durable_log_identity(store: EventStore) -> EventStore:
    """The object that actually owns the durable log behind ``store``.

    `PublishingEventStore` is a transparent decorator: it delegates every
    persistence operation to an inner store and adds only an in-process
    notification, so a runtime wrapping store X and store X itself are the same
    log. `build_default_runtime()` always wraps, so without resolving it a
    Supervisor could never agree with any runtime it did not construct itself.

    Exactly that one known decorator is unwrapped, by type. This is not a
    loosening into "looks equivalent": anything else is compared as it is, so
    two separately-configured stores are still two different logs.
    """

    seen: set[int] = set()
    while isinstance(store, PublishingEventStore):
        if id(store) in seen:  # pragma: no cover - defensive against a cycle
            break
        seen.add(id(store))
        store = store.inner
    return store


@runtime_checkable
class AgentExecution(Protocol):
    """One Agent's exclusive execution runtime.

    "Exclusive" is load-bearing: an Activation disposes this object, so two
    Activations sharing one would let either of them shut down the other.
    """

    @property
    def session_id(self) -> str:
        """The Session this execution appends to."""

    @property
    def event_store(self) -> EventStore:
        """The store this execution appends to, for identity comparison."""

    async def run_turn(self, turn_input: TurnInput) -> TurnResult:
        """Run one Turn from ``turn_input`` and return its result."""

    async def cancel_turn(self, *, reason: str) -> bool:
        """Cancel the Turn currently running, if any.

        Returns whether a Turn was actually cancelled. Must converge - the
        model call, tools and subprocesses have to be finished when this
        returns, not merely asked to stop.
        """

    async def dispose(self) -> None:
        """Release everything this execution owns. Idempotent."""


class AgentActivationFactory(Protocol):
    """Builds the execution runtime for an Agent.

    Deliberately injected rather than implemented here. A
    ``ChildProvisioningPolicy`` may approve or map the preset/workspace intent,
    but this factory remains the only seam that turns the approved ``preset``
    into a Provider, model, prompt and runtime, or the approved
    ``workspace_id`` into a concrete directory. Treating ``workspace_id`` as a
    local path, or defaulting a preset to some example, would bake one
    deployment's choices into the control plane.
    """

    async def provision(
        self,
        spec: AgentSpec,
        *,
        agent_id: str,
        session_id: str | None,
    ) -> AgentExecution:
        """Create this Agent's Session and an exclusive runtime for it.

        Called before the Agent's identity is recorded, so the returned
        execution's ``session_id`` is what gets registered. If ``session_id`` is
        given it must be used exactly; otherwise the factory assigns one.
        """

    async def activate(self, record: AgentRecord) -> AgentExecution:
        """Rebuild an exclusive runtime for an Agent that already exists."""


class AgentRuntimeExecution:
    """`AgentExecution` backed by one exclusively-owned `AgentRuntime`.

    A thin adapter, on purpose. It owns the runtime it was handed and disposes
    it, so the caller must not share that runtime with anything else.
    """

    __slots__ = ("_dispose_task", "_disposed", "_runtime", "_session_id")

    def __init__(self, runtime, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._disposed = False
        self._dispose_task: asyncio.Task[None] | None = None

    @property
    def runtime(self):
        return self._runtime

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_store(self) -> EventStore:
        # Reached through the runtime's public SessionService, not a private
        # field: the Supervisor is entitled to know where Turns are written.
        return self._runtime.sessions.store

    async def run_turn(self, turn_input: TurnInput) -> TurnResult:
        return await self._runtime.run_existing(self._session_id, turn_input)

    async def cancel_turn(self, *, reason: str) -> bool:
        return await self._runtime.cancel(self._session_id, reason=reason)

    async def dispose(self) -> None:
        if self._dispose_task is None:
            # No suspension occurs between the check and assignment, so two
            # callers on this event loop cannot create two shutdown owners.
            self._disposed = True
            self._dispose_task = asyncio.create_task(
                self._runtime.dispose(),
                name=f"traceh-runtime-execution-dispose-{self._session_id}",
            )
        task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise


__all__ = [
    "AgentActivationFactory",
    "AgentExecution",
    "AgentRuntimeExecution",
    "durable_log_identity",
]
