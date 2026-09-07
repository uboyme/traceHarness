"""Model capability limited to a bounded proposal from its own closed evidence."""

from traceh.api.json_types import fingerprint
from traceh.api.tools import EffectKind, ToolOutput
from traceh.projects.events import detached, exact, reference


class MemoryProposalTool:
    name = "propose_workspace_memory"
    effect_kind = EffectKind.EXTERNAL_TRANSACTION
    description = (
        "Propose a short stable project fact for human review. Supply exact original event IDs "
        "from already closed Turns in this Session. This never approves a fact, chooses a project "
        "or slot, grants authority, or makes the fact visible in Context."
    )
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["proposal_id", "body", "source_event_ids"],
        "properties": {
            "proposal_id": {"type": "string"},
            "body": {"type": "string"},
            "source_event_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        },
    }

    def __init__(self, propose):
        self._propose = propose

    async def execute(self, arguments, context):
        arguments = detached(arguments)
        exact(arguments, {"proposal_id", "body", "source_event_ids"})
        identity = fingerprint(
            {
                "session_id": context.session_id,
                "turn_id": context.turn_id,
                "step_id": context.step_id,
                "tool_call_id": context.tool_call_id,
            }
        )
        event = await self._propose(
            context.session_id,
            **arguments,
            operation_id=f"memory-tool:{identity}",
            actor_id=f"model-tool:{identity}",
        )
        return ToolOutput(
            content="Memory proposed; awaiting an exact host decision.",
            data={
                "status": "proposed",
                "proposal_ref": reference(event),
                "proposal_digest": event.data["proposal_digest"],
            },
        )
