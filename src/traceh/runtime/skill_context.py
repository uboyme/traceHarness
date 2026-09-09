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

    async def read(self, session_id):
        from traceh.session.skill_selection import eligible_skills, project_selection

        workspace = await self._sessions.workspace_for(session_id)
        operation = f"skill-inspection:{uuid4()}"
        async with self._compositions.lease(
            workspace=workspace,
            session_id=session_id,
            turn_id=operation,
            step_id=operation,
        ) as active:
            if active.tools.sessions is not self._sessions:
                raise ValueError("skill-context-owner-mismatch")
            events = await self._sessions.read_skill_selection(session_id)
            selection = project_selection(events, session_id)
            eligible, reason = eligible_skills(selection, active.snapshot)
            return {
                "catalog_digest": active.snapshot.skill_catalog_digest,
                "catalog": [item.to_dict() for item in active.snapshot.skill_catalog],
                "selection": selection,
                "selection_head": len(events),
                "eligible": [item.skill_id for item in eligible],
                "reason": reason,
                "retrieval_enabled": self._policy is not None,
                "capabilities": {
                    "tools": [tool.name for tool in active.snapshot.tools],
                    "provider": active.snapshot.provider,
                    "policies": list(active.snapshot.policies),
                    "middlewares": list(active.snapshot.tool_middlewares),
                    "composition_revision": active.snapshot.revision,
                },
            }

    async def select(
        self,
        session_id,
        *,
        operation_id,
        expected_head,
        actor_id,
        skills,
        expected_catalog_digest=None,
    ):
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
            if (
                expected_catalog_digest is not None
                and active.snapshot.skill_catalog_digest != expected_catalog_digest
            ):
                raise ValueError("skill-selection-review-stale")
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
            await self._sessions.rebuild_context_index(corpus)
            return json.loads(corpus.manifest_json)
