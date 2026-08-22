"""Process-local coordination for durable Agent lifecycle ownership.

The ownership relation is not another registry.  It is projected from
``AgentRecord.owner_agent_id`` every time a lifecycle decision needs durable
facts.  This module adds only process-local coordination around that fact:

* an admission lease says which durable ownership lineage one operation may
  activate;
* a disposal scope closes one subtree to new admissions and waits for older
  admissions in that subtree to leave;
* close blocks every future admission and waits for all admitted work.

Message sources and ``forked_from_session_id`` are intentionally absent.  They
describe communication and history lineage, not cleanup responsibility.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from traceh.agents import AgentDirectory


class AgentOwnershipGraphError(ValueError):
    """A supplied ownership graph is contradictory.

    ``AgentDirectory`` already rejects these states during replay.  The guard
    remains here because the graph is a public, independently testable kernel
    primitive and must not silently reinterpret a malformed input.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the agent lifecycle ownership graph is invalid")


class LifecycleAdmissionClosed(RuntimeError):
    """The process-local coordinator has entered final close."""


class AgentOwnershipGraph:
    """Immutable lifecycle ownership projected from one Directory view."""

    __slots__ = ("_children", "_order", "_owner")

    def __init__(self, directory: AgentDirectory) -> None:
        records = directory.records
        order = tuple(record.agent_id for record in records)
        if len(set(order)) != len(order):
            raise AgentOwnershipGraphError("agent-owner-id-duplicate")

        owner = {record.agent_id: record.owner_agent_id for record in records}
        children: dict[str, list[str]] = {agent_id: [] for agent_id in order}
        for agent_id in order:
            owner_id = owner[agent_id]
            if owner_id is None:
                continue
            if owner_id == agent_id:
                raise AgentOwnershipGraphError("agent-owner-self")
            if owner_id not in owner:
                raise AgentOwnershipGraphError("agent-owner-unknown")
            children[owner_id].append(agent_id)

        # Do not rely on creation order for cycle safety.  The Directory
        # currently enforces owner-before-child, but this primitive should keep
        # its own contract if that projector is ever replaced.
        for agent_id in order:
            seen: set[str] = set()
            cursor: str | None = agent_id
            while cursor is not None:
                if cursor in seen:
                    raise AgentOwnershipGraphError("agent-owner-cycle")
                seen.add(cursor)
                cursor = owner[cursor]

        self._order = order
        self._owner = owner
        self._children = {key: tuple(value) for key, value in children.items()}

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._owner

    def lineage(self, agent_id: str) -> tuple[str, ...]:
        """Return root-to-Agent lifecycle ownership, or ``()`` if unknown."""

        if agent_id not in self._owner:
            return ()
        reverse: list[str] = []
        cursor: str | None = agent_id
        while cursor is not None:
            reverse.append(cursor)
            cursor = self._owner[cursor]
        reverse.reverse()
        return tuple(reverse)

    def lineage_for_new(self, agent_id: str, owner_agent_id: str | None) -> tuple[str, ...]:
        """Return the admission lineage for an identity not yet persisted."""

        if owner_agent_id is None:
            return (agent_id,)
        owner_lineage = self.lineage(owner_agent_id)
        if not owner_lineage:
            raise AgentOwnershipGraphError("agent-owner-unknown")
        return (*owner_lineage, agent_id)

    def subtree_postorder(self, agent_id: str) -> tuple[str, ...]:
        """Return descendants before their owner, preserving creation order."""

        if agent_id not in self._owner:
            return ()
        result: list[str] = []
        stack: list[tuple[str, bool]] = [(agent_id, False)]
        while stack:
            current, expanded = stack.pop()
            if expanded:
                result.append(current)
                continue
            stack.append((current, True))
            for child in reversed(self._children[current]):
                stack.append((child, False))
        return tuple(result)

    def forest_postorder(self) -> tuple[str, ...]:
        """Return every Agent child-first, with roots in creation order."""

        result: list[str] = []
        for agent_id in self._order:
            if self._owner[agent_id] is None:
                result.extend(self.subtree_postorder(agent_id))
        return tuple(result)


class AgentLifecycleCoordinator:
    """Linearizes admissions against subtree and whole-process disposal."""

    __slots__ = ("_admissions", "_closing", "_condition", "_next_token", "_scopes")

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._admissions: dict[int, frozenset[str]] = {}
        self._scopes: dict[int, frozenset[str]] = {}
        self._next_token = 0
        self._closing = False

    def _token(self) -> int:
        self._next_token += 1
        return self._next_token

    @asynccontextmanager
    async def admission(self, lineage: Iterable[str]) -> AsyncIterator[None]:
        """Admit activation work unless an intersecting subtree is closing."""

        members = frozenset(lineage)
        if not members:
            raise AgentOwnershipGraphError("agent-owner-lineage-empty")
        token: int | None = None
        async with self._condition:
            while True:
                if self._closing:
                    raise LifecycleAdmissionClosed()
                if not any(members & scope for scope in self._scopes.values()):
                    token = self._token()
                    self._admissions[token] = members
                    break
                await self._condition.wait()
        try:
            yield
        finally:
            assert token is not None
            async with self._condition:
                self._admissions.pop(token, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def disposal(
        self, affected: Iterable[str]
    ) -> AsyncIterator[LifecycleDisposalBarrier]:
        """Close one subtree and expose a barrier for older admissions.

        Registration and waiting are deliberately separate.  Once the scope is
        registered, the Supervisor may cancel candidate builds that Stage C
        already promised disposal would converge; it then waits on the yielded
        barrier before touching any installed Activation.
        """

        members = frozenset(affected)
        if not members:
            raise AgentOwnershipGraphError("agent-owner-subtree-empty")
        token: int | None = None
        async with self._condition:
            while any(members & scope for scope in self._scopes.values()):
                await self._condition.wait()
            token = self._token()
            self._scopes[token] = members
        try:
            yield LifecycleDisposalBarrier(self, members)
        finally:
            assert token is not None
            async with self._condition:
                self._scopes.pop(token, None)
                self._condition.notify_all()

    async def begin_close(self) -> None:
        """Permanently reject new admission attempts."""

        async with self._condition:
            self._closing = True
            self._condition.notify_all()

    async def wait_quiescent(self) -> None:
        """Wait until every already-admitted operation has left."""

        async with self._condition:
            while self._admissions:
                await self._condition.wait()

    async def _wait_subtree_quiescent(self, members: frozenset[str]) -> None:
        async with self._condition:
            while any(members & item for item in self._admissions.values()):
                await self._condition.wait()


class LifecycleDisposalBarrier:
    """The quiescence barrier belonging to one registered disposal scope."""

    __slots__ = ("_coordinator", "_members")

    def __init__(
        self, coordinator: AgentLifecycleCoordinator, members: frozenset[str]
    ) -> None:
        self._coordinator = coordinator
        self._members = members

    async def wait_quiescent(self) -> None:
        await self._coordinator._wait_subtree_quiescent(self._members)


__all__ = [
    "AgentLifecycleCoordinator",
    "AgentOwnershipGraph",
    "AgentOwnershipGraphError",
    "LifecycleDisposalBarrier",
    "LifecycleAdmissionClosed",
]
