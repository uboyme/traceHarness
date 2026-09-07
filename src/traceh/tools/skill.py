"""A bounded receipt Tool; Skill reference bytes never become a Tool result."""

from traceh.api.tools import EffectKind, ToolOutput
from traceh.session.skill_requests import REQUEST_KEYS, SKILL_TOOL_NAME, receipt_for


class SkillDisclosureTool:
    name = SKILL_TOOL_NAME
    effect_kind = EffectKind.PURE_READ
    description = (
        "Request a previously disclosed, host-selected Skill reference for the next Step. "
        "Copy its skill_id, version and catalog_digest. Start with directory to discover section "
        "and resource chunk IDs. Only a bounded receipt and declared IDs are returned, never body "
        "text. This cannot select Skills, enable plugins or grant tools."
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
            "section_id": {"type": ["string", "null"]},
            "resource_id": {"type": ["string", "null"]},
            "chunk_id": {"type": ["string", "null"]},
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
            content="Reference request accepted for this Turn's immediate next Step only.",
            data={"skill_receipt": receipt, "available": available},
        )
