"""A read-only Tool that records a bounded History request receipt."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from traceh.api.events import EventEnvelope
from traceh.api.history import HistoryPageRequest, HistoryReadPolicy
from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.session.history_requests import HISTORY_TOOL_NAME, validate_model_history_request


class HistoryDisclosureTool:
    name = HISTORY_TOOL_NAME
    description = (
        "Request one already disclosed page of this Session's compacted history. "
        "To locate a fact in unknown pages, prefer search_history when available; "
        "then use its exact hit cursor here if the snippet is insufficient. "
        "Only a receipt is returned; the immediate next Step may receive the page as "
        "historical reference in the LAST user message's current host reference context. "
        "Admitted pages may remain within this Turn while within budget, with current freshness "
        "labels. Use exactly a disclosed cursor, never a sequence range."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "block_id": {"type": "string"},
            "cursor": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "policy_digest": {"type": "string"},
                    "index": {"type": "integer", "minimum": 0},
                },
                "required": ["block_id", "policy_digest", "index"],
                "additionalProperties": False,
            },
            "requested_tier": {"type": "string", "enum": ["section", "chunk"]},
        },
        "required": ["block_id", "cursor", "requested_tier"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        read_session: Callable[[str], Awaitable[tuple[EventEnvelope, ...]]],
        *,
        policy: HistoryReadPolicy,
    ) -> None:
        self._read_session = read_session
        self._policy = policy

    async def execute(
        self,
        arguments: dict[str, JsonValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        request = HistoryPageRequest.from_dict(arguments)
        events = await self._read_session(context.session_id)
        receipt = validate_model_history_request(
            events,
            context=context,
            request=request,
            policy=self._policy,
        )
        return ToolOutput(
            content=(
                "History page accepted for this Turn's immediate next Step. Read the LAST "
                "user message's current reference. Admitted pages may remain this Turn "
                "within budget; check freshness. This receipt contains no page body."
            ),
            data={"history_receipt": receipt},
        )
