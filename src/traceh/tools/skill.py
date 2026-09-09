"""A bounded receipt Tool; Skill reference bytes never become a Tool result."""

from traceh.api.tools import EffectKind, ToolOutput
from traceh.session.skill_requests import REQUEST_KEYS, SKILL_TOOL_NAME, receipt_for


class SkillDisclosureTool:
    name = SKILL_TOOL_NAME
    effect_kind = EffectKind.PURE_READ
    description = (
        "Request a previously disclosed, host-selected Skill reference for the next Step. "
        "Copy its id (skill_id), version and catalog_digest from the reference. "
        "If you only have a summary, request directory to discover chapter titles, descriptions, "
        "IDs and byte sizes. Choose the relevant section or resource chunk by its description; "
        "IDs are opaque handles, not search queries. Set unused IDs to JSON null without quotes; "
        "a string spelling null is invalid. "
        "The result contains only a receipt and navigation metadata. Requested body text appears "
        "in the NEXT model request's LAST user message (current host reference context). "
        "Admitted bodies remain within this Turn while authorized and within budget; read it there "
        "before answering. If excluded by the host budget, report missing evidence. "
        "This cannot select Skills, enable plugins or grant tools."
    )
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(REQUEST_KEYS),
        "properties": {
            "skill_id": {"type": "string"},
            "version": {"type": "string"},
            "catalog_digest": {"type": "string"},
            "requested_tier": {
                "type": "string",
                "enum": ["directory", "summary", "section", "chunk"],
            },
            "section_id": {
                "type": ["string", "null"],
                "description": (
                    "Required for section only; MUST be JSON null without quotes for chunk, "
                    "directory and summary."
                ),
            },
            "resource_id": {
                "type": ["string", "null"],
                "description": (
                    "Required for chunk only; MUST be JSON null without quotes for section, "
                    "directory and summary."
                ),
            },
            "chunk_id": {
                "type": ["string", "null"],
                "description": (
                    "Required with resource_id for chunk; MUST be JSON null without quotes "
                    "for every other tier."
                ),
            },
        },
    }

    def __init__(self, read_session, read_selection, *, policy):
        self._read_session = read_session
        self._read_selection = read_selection
        self._policy = policy

    async def execute(self, arguments, context):
        receipt, available = receipt_for(
            await self._read_session(context.session_id),
            context=context,
            request=arguments,
            policy=self._policy,
            selections=await self._read_selection(context.session_id),
        )
        return ToolOutput(
            content=(
                "Reference request accepted for this Turn's immediate next Step. "
                "The LAST user message in your next request is refreshed with the requested "
                "tier/body. Read the current reference before answering or requesting more. "
                "Admitted bodies may remain within this Turn while authorized and within budget. "
                "This receipt contains navigation metadata, not body text."
            ),
            data={"skill_receipt": receipt, "available": available},
        )
