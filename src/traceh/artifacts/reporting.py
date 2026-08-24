"""Read-only AgentSupervisor adapter exposing already-recorded Artifact refs."""

from __future__ import annotations

from dataclasses import replace

from traceh.api.agents import (
    AgentHandle,
    AgentMessage,
    AgentRunReport,
    AgentSpec,
    AgentSupervisor,
    MessageReceipt,
    MessageTarget,
)
from traceh.artifacts.errors import ArtifactInputError
from traceh.artifacts.reader import PatchArtifactReader
from traceh.session.event_store import EventStore
from traceh.supervision.execution import durable_log_identity


class ArtifactReportingAgentSupervisor:
    """Decorate reports without capturing, scheduling or caching anything."""

    __slots__ = ("_inner", "_reader")

    def __init__(
        self, inner: AgentSupervisor, reader: PatchArtifactReader
    ) -> None:
        if durable_log_identity(inner.store) is not durable_log_identity(reader.store):
            raise ArtifactInputError("artifact-store-mismatch", "store")
        self._inner = inner
        self._reader = reader

    @property
    def store(self) -> EventStore:
        return self._inner.store

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentHandle:
        return await self._inner.create(
            spec,
            request_id=request_id,
            agent_id=agent_id,
            session_id=session_id,
        )

    async def resume(self, session_id: str) -> AgentHandle:
        return await self._inner.resume(session_id)

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        return await self._inner.send(
            agent_id, message, target=target, wakeup=wakeup
        )

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        return await self._inner.interrupt(agent_id, reason)

    async def wait_idle(self, agent_id: str) -> None:
        await self._inner.wait_idle(agent_id)

    async def wait_message(
        self, agent_id: str, message_id: str
    ) -> AgentRunReport:
        return await self._with_refs(
            await self._inner.wait_message(agent_id, message_id)
        )

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        return await self._with_refs(await self._inner.report(agent_id, message_id))

    async def dispose(self, agent_id: str) -> None:
        await self._inner.dispose(agent_id)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _with_refs(self, report: AgentRunReport) -> AgentRunReport:
        if report.message_id is None:
            return report
        recorded = await self._reader.refs_for(report.agent_id, report.message_id)
        refs = tuple(dict.fromkeys((*report.artifact_refs, *recorded)))
        return replace(report, artifact_refs=refs)


__all__ = ["ArtifactReportingAgentSupervisor"]
