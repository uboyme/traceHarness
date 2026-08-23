"""Durable authorization for model-visible Agent control tools.

Authorization is a projection, not a cache.  Every decision loads a fresh
``AgentDirectory`` through ``AgentDirectoryReader`` and derives ownership from
that immutable view.  It never reaches into a live Supervisor, so a future
managed control plane can implement :class:`traceh.api.agents.AgentSupervisor`
without inheriting ``ProcessAgentSupervisor`` internals.
"""

from __future__ import annotations

from traceh.agents import AgentDirectory, AgentDirectoryReader
from traceh.agents.identity import require_identifier
from traceh.api.agents import AgentRecord
from traceh.session.event_store import EventStore
from traceh.supervision.lifecycle import AgentOwnershipGraph


class AgentToolBindingError(RuntimeError):
    """The host bound Agent tools to a different durable identity boundary."""


class AgentToolAuthorizationError(PermissionError):
    """A bound Agent tried to operate outside its ownership subtree."""


class AgentToolAuthority:
    """Checks caller Session and strict-descendant authority from durable facts.

    The instance stores only a reader and the host-bound owner id.  It does not
    retain an ``AgentDirectory``, ownership graph, authorization result or live
    Activation, so a later durable child is visible on the next decision.
    """

    __slots__ = ("_directory_reader", "_owner_agent_id")

    def __init__(
        self,
        *,
        directory_reader: AgentDirectoryReader,
        owner_agent_id: str,
    ) -> None:
        self._directory_reader = directory_reader
        self._owner_agent_id = require_identifier(
            owner_agent_id, field="owner_agent_id"
        )

    @property
    def owner_agent_id(self) -> str:
        return self._owner_agent_id

    @property
    def store(self) -> EventStore:
        return self._directory_reader.store

    @staticmethod
    def _require_bound_owner(
        directory: AgentDirectory,
        *,
        owner_agent_id: str,
        caller_session_id: str,
    ) -> AgentRecord:
        owner = directory.get(owner_agent_id)
        if owner is None or owner.session_id != caller_session_id:
            raise AgentToolBindingError(
                "the subagent tools are running outside their bound Agent Session"
            )
        return owner

    async def require_caller(self, caller_session_id: str) -> AgentRecord:
        """Return the freshly replayed owner record for this Tool invocation."""

        directory = await self._directory_reader.load()
        return self._require_bound_owner(
            directory,
            owner_agent_id=self._owner_agent_id,
            caller_session_id=caller_session_id,
        )

    async def require_owned(
        self,
        target_agent_id: str,
        caller_session_id: str,
    ) -> AgentRecord:
        """Require ``target_agent_id`` to be a strict owned descendant.

        Caller binding and descendant authorization use the same fresh
        Directory snapshot.  The caller itself, its ancestors, siblings and a
        separate ownership tree are therefore all rejected.
        """

        target_agent_id = require_identifier(target_agent_id, field="agent_id")
        directory = await self._directory_reader.load()
        self._require_bound_owner(
            directory,
            owner_agent_id=self._owner_agent_id,
            caller_session_id=caller_session_id,
        )
        target = directory.get(target_agent_id)
        lineage = AgentOwnershipGraph(directory).lineage(target_agent_id)
        if (
            target is None
            or not lineage
            or self._owner_agent_id not in lineage[:-1]
        ):
            raise AgentToolAuthorizationError(
                "the target Agent is outside the caller's ownership subtree"
            )
        return target


__all__ = [
    "AgentToolAuthority",
    "AgentToolAuthorizationError",
    "AgentToolBindingError",
]
