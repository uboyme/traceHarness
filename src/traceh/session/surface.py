"""Projection from durable events to model-visible messages."""

from __future__ import annotations

from traceh.api.events import EventEnvelope
from traceh.api.llm import ModelMessage
from traceh.session.product_context import latest_product_context
from traceh.session.surface_replacement import surface_conversation


class SurfaceProjector:
    def project(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        through_seq: int | None = None,
    ) -> tuple[ModelMessage, ...]:
        selected = tuple(
            event
            for event in events
            if through_seq is None or event.seq <= through_seq
        )
        # A replacement is appended after the history it replaces, so it is
        # projected at that history's logical position rather than at its own
        # sequence. Ordering by append order would put a summary of old turns
        # behind newer conversation - including behind the current user
        # message - and describe a conversation that never happened.
        conversation = tuple(
            entry.message for entry in surface_conversation(selected)
        )
        product_context = latest_product_context(selected)
        if product_context is None:
            return conversation
        # Current host evidence and its bounded historical reference describe
        # the conversation rather than adding chronological utterances.  Keep
        # them atomically ahead of the original history so stale assistant prose
        # cannot outrank the durable current task head.
        return (*product_context[1].messages, *conversation)
