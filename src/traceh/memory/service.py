"""Append-only host authority and a separate, proposal-only model entry point."""

from traceh.api.memory import MemoryPolicy
from traceh.memory.projection import decision_digest, memory_stream, proposal_digest, replay_memory
from traceh.memory.sources import session_source, validate_sources
from traceh.projects.events import (
    append_owned,
    control_data,
    detached,
    identifier,
    referenced,
)
from traceh.projects.service import ProjectScopeService


class MemoryService:
    """Store-owned control service; neither a Runtime lifecycle nor a retrieval cache."""

    def __init__(self, scope: ProjectScopeService, policy: MemoryPolicy):
        if type(scope) is not ProjectScopeService or type(policy) is not MemoryPolicy:
            raise ValueError("memory-owner-or-policy-invalid")
        self.scope = scope
        self.policy = policy
        self.store = scope.store

    async def read(self, session_id):
        binding = await self.scope.resolve(session_id)
        project_id = binding.data["project_id"]
        view = replay_memory(
            project_id, await self.store.read(memory_stream(project_id)), self.policy
        )
        for proposal in view.proposals:
            await validate_sources(self.scope, project_id, proposal.data["sources"], self.policy)
        if await self.store.head(memory_stream(project_id)) != view.head:
            raise ValueError("memory-source-unavailable")
        return view

    async def _append(self, kind, data):
        project_id = data["project_id"]
        return await append_owned(
            self.store,
            memory_stream(project_id),
            kind,
            data,
            lambda events: replay_memory(project_id, events, self.policy),
        )

    async def propose(
        self,
        session_id,
        *,
        proposal_id,
        body,
        sources,
        operation_id,
        actor_id,
        expected_head,
    ):
        """Host evidence proposal. Human declarations are captured only by declare()."""
        sources = detached(sources)
        if type(sources) is not list or any(
            type(source) is not dict or source.get("kind") == "host-declaration"
            for source in sources
        ):
            raise ValueError("memory-declaration-host-only")
        return await self._propose(
            session_id,
            proposal_id=proposal_id,
            body=body,
            sources=sources,
            operation_id=operation_id,
            actor_id=actor_id,
            expected_head=expected_head,
        )

    async def declare(
        self,
        session_id,
        *,
        proposal_id,
        body,
        statement,
        declaration_id,
        operation_id,
        actor_id,
        expected_head,
    ):
        """Capture an actual host declaration as evidence, still only proposed."""
        return await self._propose(
            session_id,
            proposal_id=proposal_id,
            body=body,
            sources=[
                {
                    "kind": "host-declaration",
                    "declaration_id": identifier(declaration_id),
                    "actor_id": identifier(actor_id),
                    "statement": statement,
                }
            ],
            operation_id=operation_id,
            actor_id=actor_id,
            expected_head=expected_head,
        )

    async def _propose(
        self,
        session_id,
        *,
        proposal_id,
        body,
        sources,
        operation_id,
        actor_id,
        expected_head,
    ):
        fields = detached(
            {"proposal_id": identifier(proposal_id), "body": body, "sources": sources}
        )
        binding = await self.scope.resolve(session_id)
        data = control_data(
            binding.data["project_id"], operation_id, actor_id, expected_head, **fields
        )
        data["proposal_digest"] = proposal_digest(data)
        await validate_sources(self.scope, data["project_id"], fields["sources"], self.policy)
        return await self._append("memory/proposed", data)

    async def propose_model(
        self, session_id, *, proposal_id, body, source_event_ids, operation_id, actor_id
    ):
        """Narrow Tool callback: current Session only, no project/slot/decision/source variant."""
        source_event_ids = detached(source_event_ids)
        source = await session_source(self.scope, session_id, source_event_ids, self.policy)
        binding = await self.scope.resolve(session_id)
        view = replay_memory(
            binding.data["project_id"],
            await self.store.read(memory_stream(binding.data["project_id"])),
            self.policy,
        )
        # Re-delivery of the same Tool call uses its original head, never a new operation.
        prior = next((e for e in view.events if e.data["operation_id"] == operation_id), None)
        expected_head = prior.data["expected_head"] if prior else view.head
        return await self.propose(
            session_id,
            proposal_id=proposal_id,
            body=body,
            sources=[source],
            operation_id=operation_id,
            actor_id=actor_id,
            expected_head=expected_head,
        )

    async def approve(
        self,
        session_id,
        *,
        proposal_ref,
        proposal_digest,
        memory_id,
        fact_slot,
        operation_id,
        actor_id,
        expected_head,
    ):
        return await self._decide(
            session_id,
            "memory/approved",
            operation_id,
            actor_id,
            expected_head,
            proposal_ref=proposal_ref,
            proposal_digest=proposal_digest,
            memory_id=memory_id,
            fact_slot=fact_slot,
        )

    async def supersede(
        self,
        session_id,
        *,
        proposal_ref,
        proposal_digest,
        memory_id,
        fact_slot,
        predecessor_ref,
        predecessor_digest,
        operation_id,
        actor_id,
        expected_head,
    ):
        return await self._decide(
            session_id,
            "memory/superseded",
            operation_id,
            actor_id,
            expected_head,
            proposal_ref=proposal_ref,
            proposal_digest=proposal_digest,
            memory_id=memory_id,
            fact_slot=fact_slot,
            predecessor_ref=predecessor_ref,
            predecessor_digest=predecessor_digest,
        )

    async def revoke(
        self,
        session_id,
        *,
        memory_id,
        fact_slot,
        predecessor_ref,
        predecessor_digest,
        operation_id,
        actor_id,
        expected_head,
    ):
        return await self._decide(
            session_id,
            "memory/revoked",
            operation_id,
            actor_id,
            expected_head,
            memory_id=memory_id,
            fact_slot=fact_slot,
            predecessor_ref=predecessor_ref,
            predecessor_digest=predecessor_digest,
        )

    async def _decide(self, session_id, kind, operation_id, actor_id, expected_head, **fields):
        fields = detached(fields)
        view = await self.read(session_id)
        data = control_data(view.project_id, operation_id, actor_id, expected_head, **fields)
        if "proposal_ref" in fields:
            proposal = referenced(view.events, fields["proposal_ref"])
            if (
                proposal.type != "memory/proposed"
                or fields["proposal_digest"] != proposal.data["proposal_digest"]
            ):
                raise ValueError("memory-approval-mismatch")
        data["decision_digest"] = decision_digest(data)
        return await self._append(kind, data)


__all__ = ["MemoryService"]
