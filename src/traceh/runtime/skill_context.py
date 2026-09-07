"""Host control surface borrowing the Runtime's existing Session and Lease owners."""

import json
from uuid import uuid4

from traceh.api.json_types import canonical_json
from traceh.session.skill_retrieval import prepare_corpus
from traceh.session.skill_selection import set_selection


class SkillContextControl:
    def __init__(self, sessions, compositions, policy):
        self._sessions = sessions
        self._compositions = compositions
        self._policy = policy

    async def select(self, session_id, *, operation_id, expected_head, actor_id, skills):
        if self._policy is None:
            raise ValueError("skill-retrieval-disabled")
        skills = tuple(json.loads(canonical_json(list(skills))))
        workspace = await self._sessions.workspace_for(session_id)
        async with self._compositions.lease(
            workspace=workspace,
            session_id=session_id,
            turn_id=f"skill-selection:{operation_id}",
            step_id=f"skill-selection:{operation_id}",
        ) as active:
            if active.tools.sessions is not self._sessions:
                raise ValueError("skill-context-owner-mismatch")
            return await set_selection(
                self._sessions,
                session_id,
                active.snapshot,
                operation_id=operation_id,
                expected_head=expected_head,
                actor_id=actor_id,
                skills=skills,
            )

    async def rebuild_index(self, session_id):
        if self._policy is None:
            raise ValueError("skill-retrieval-disabled")
        workspace = await self._sessions.workspace_for(session_id)
        operation = str(uuid4())
        async with self._compositions.lease(
            workspace=workspace,
            session_id=session_id,
            turn_id=f"skill-index:{operation}",
            step_id=f"skill-index:{operation}",
        ) as active:
            if active.tools.sessions is not self._sessions:
                raise ValueError("skill-context-owner-mismatch")
            selections = await self._sessions.read_skill_selection(session_id)
            corpus, _, _, _, reason = prepare_corpus(
                active.snapshot, selections, session_id, self._policy
            )
            if reason is not None:
                raise ValueError(f"skill-index-{reason}")
            return await self._sessions.rebuild_context_index(corpus)
