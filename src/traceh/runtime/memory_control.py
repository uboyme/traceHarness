"""Host operations borrow the existing composition lease and exact Session owner."""

from uuid import uuid4

from traceh.projects.events import detached


class MemoryControl:
    def __init__(self, sessions, compositions, authority):
        self._sessions = sessions
        self._compositions = compositions
        self._authority = authority

    async def _run(self, session_id, name, arguments):
        if self._authority is None:
            raise ValueError("project-memory-disabled")
        arguments = detached(arguments)
        workspace = await self._sessions.workspace_for(session_id)
        operation = f"memory-control:{uuid4()}"
        async with self._compositions.lease(
            workspace=workspace,
            session_id=session_id,
            turn_id=operation,
            step_id=operation,
        ) as active:
            if (
                active.tools.sessions is not self._sessions
                or self._authority.scope.sessions is not self._sessions
            ):
                raise ValueError("memory-context-owner-mismatch")
            return await getattr(self._authority, name)(session_id, **arguments)

    async def read(self, session_id):
        return await self._run(session_id, "read", {})

    async def declare(self, session_id, **arguments):
        return await self._run(session_id, "declare", arguments)

    async def propose(self, session_id, **arguments):
        return await self._run(session_id, "propose", arguments)

    async def approve(self, session_id, **arguments):
        return await self._run(session_id, "approve", arguments)

    async def supersede(self, session_id, **arguments):
        return await self._run(session_id, "supersede", arguments)

    async def revoke(self, session_id, **arguments):
        return await self._run(session_id, "revoke", arguments)
