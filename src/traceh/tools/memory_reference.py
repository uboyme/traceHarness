"""Receipt-only Memory disclosure; no approval or raw source-reading capability."""

from traceh.api.tools import EffectKind, ToolOutput
from traceh.session.memory_requests import MEMORY_TOOL_NAME, REQUEST_KEYS, receipt_for


class MemoryDisclosureTool:
    name = MEMORY_TOOL_NAME
    effect_kind = EffectKind.PURE_READ
    description = (
        "Request the exact active Memory already disclosed in this Step for the immediate next "
        "Step. Copy memory_id and version; choose directory, summary or section. Summary "
        "and section disclose the complete approved short fact. Returns a receipt, never body "
        "text or original cross-Session sources. Read the actual body in the LAST user message's "
        "current host reference context; it may remain within this Turn while active and within "
        "budget. Does not approve, revoke or change project scope."
    )
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(REQUEST_KEYS),
        "properties": {
            "memory_id": {"type": "string"},
            "version": {"type": "string"},
            "requested_tier": {"type": "string", "enum": ["directory", "summary", "section"]},
        },
    }

    def __init__(self, read_session, read_memory, recheck_memory, *, policy):
        self._read_session = read_session
        self._read_memory = read_memory
        self._recheck_memory = recheck_memory
        self._policy = policy

    async def execute(self, arguments, context):
        events = await self._read_session(context.session_id)
        source = await self._read_memory(context.session_id)
        receipt = receipt_for(
            events, context=context, request=arguments, policy=self._policy, source=source
        )
        if not await self._recheck_memory(source):
            raise ValueError("memory-request-source-changed")
        return ToolOutput(
            content=(
                "Memory reference accepted for this Turn's immediate next Step. Read the LAST "
                "user message's current host reference context. Admitted bodies may remain "
                "within this Turn while active and within budget."
            ),
            data={"memory_receipt": receipt},
        )
