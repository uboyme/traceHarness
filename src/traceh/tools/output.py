"""Read exact retained output through the normal Tool admission boundary."""

from __future__ import annotations

from traceh.api.json_types import canonical_json
from traceh.api.tools import EffectKind, ToolOutput
from traceh.session.service import SessionService
from traceh.session.tool_output import (
    OUTPUT_READ_TOOL,
    OUTPUT_SEARCH_TOOL,
    render_output_page,
    render_output_search,
    resolve_tool_output,
)


class ReadToolOutput:
    name = OUTPUT_READ_TOOL
    description = (
        "Read original retained Tool output from the current Session, including after restart "
        "or history compaction. Copy effect_id and digest from output_ref. part=content reads "
        "original text; part=data reads original structured JSON. Offsets count Unicode "
        "characters. Continue at next_offset when more evidence is needed. This never "
        "re-executes the original tool and cannot read another Session."
        " If the reference is no longer visible, use list_tool_outputs first."
        " For a keyword use search_tool_output first; expand a match using its read_action. "
        "Its match_start is the exact keyword position."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "effect_id": {"type": "string", "minLength": 1},
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "part": {"type": "string", "enum": ["content", "data"]},
            "offset": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["effect_id", "digest"],
        "additionalProperties": False,
    }

    def __init__(self, sessions: SessionService, *, max_chars: int) -> None:
        self._sessions = sessions
        self._max_chars = max_chars

    async def execute(self, arguments, context) -> ToolOutput:
        if await self._sessions.workspace_for(context.session_id) != context.workspace:
            raise ValueError("tool-output-workspace-mismatch")
        payload = resolve_tool_output(
            await self._sessions.read_session(context.session_id),
            await self._sessions.read_effects(context.session_id),
            session_id=context.session_id,
            effect_id=arguments["effect_id"],
            digest=arguments["digest"],
        )
        content = render_output_page(
            payload,
            effect_id=arguments["effect_id"],
            digest=arguments["digest"],
            part=arguments.get("part", "content"),
            offset=arguments.get("offset", 0),
            count=arguments.get("count", self._max_chars),
            max_chars=self._max_chars,
        )
        return ToolOutput(content)


class ListToolOutputs:
    name = "list_tool_outputs"
    description = (
        "List retained Tool outputs from the current Session, newest first, including after "
        "history compaction or restart. Returns output_ref identities for search_tool_output "
        "and read_tool_output. "
        "Use this when the old output reference is no longer visible. Does not read current "
        "workspace files or execute the original tools. For more entries pass next_offset "
        "as offset and the returned through_seq to keep the same directory snapshot."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1},
            "through_seq": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }

    def __init__(self, sessions: SessionService, *, max_chars: int) -> None:
        self._sessions = sessions
        self._max_chars = max_chars

    async def execute(self, arguments, context) -> ToolOutput:
        if await self._sessions.workspace_for(context.session_id) != context.workspace:
            raise ValueError("tool-output-workspace-mismatch")
        events = await self._sessions.read_session(context.session_id)
        effects = await self._sessions.read_effects(context.session_id)
        through_seq = arguments.get("through_seq", events[-1].seq)
        if type(through_seq) is not int or not 1 <= through_seq <= events[-1].seq:
            raise ValueError("tool-output-directory-source-invalid")
        if arguments.get("offset", 0) and "through_seq" not in arguments:
            raise ValueError("tool-output-directory-source-required")
        sources = [
            event
            for event in reversed(events[:through_seq])
            if event.type == "tool/result" and "output_ref" in event.data
        ]
        offset, count = arguments.get("offset", 0), arguments.get("count", len(sources) or 1)
        if type(offset) is not int or not 0 <= offset <= len(sources):
            raise ValueError("tool-output-offset-invalid")
        if type(count) is not int or count < 1:
            raise ValueError("tool-output-count-invalid")
        entries = []

        def render():
            end = offset + len(entries)
            return canonical_json(
                {
                    "outputs": entries,
                    "offset": offset,
                    "through_seq": through_seq,
                    "next_offset": end if end < len(sources) else None,
                }
            )

        for source in sources[offset : offset + count]:
            reference = source.data["output_ref"]
            resolve_tool_output(
                events,
                effects,
                session_id=context.session_id,
                effect_id=reference["effect_id"],
                digest=reference["digest"],
            )
            entries.append(
                {
                    "output_ref": reference,
                    "tool_name": source.data["tool_name"],
                    "tool_call_id": source.data["tool_call_id"],
                    "result_seq": source.seq,
                    "status": source.data["status"],
                }
            )
            if len(render()) > self._max_chars:
                entries.pop()
                break
        content = render()
        if len(content) > self._max_chars or (not entries and offset < len(sources)):
            raise ValueError("tool-output-page-budget-too-small")
        return ToolOutput(content)


class SearchToolOutput:
    name = OUTPUT_SEARCH_TOOL
    description = (
        "Grep a literal keyword in one original retained Tool output from this Session. "
        "Use an existing output_ref or list_tool_outputs to obtain effect_id and digest. "
        "query is literal text, NOT regex; case_sensitive defaults to true. part=content "
        "searches original text; part=data searches canonical structured JSON. Returns "
        "non-overlapping matches, 1-based LF line numbers, Unicode character offsets and "
        "matched_lines separately from before_context/after_context (default 0 context lines, "
        "like grep; request context_lines explicitly). "
        "Long lines may have context_truncated=true. "
        "Nearby context can belong to DIFFERENT records. A matching header does not prove "
        "that preceding fields belong to it; read onward to verify the target record's "
        "body and boundaries before attributing values. "
        "Answer from sufficient evidence; otherwise call read_tool_output with the same "
        "effect_id/digest/part using the match's ready read_action (starts at matched lines, "
        "not preceding context). Continue searching with next_offset "
        "and the same query/options. Empty matches means this literal was not found in "
        "the searched suffix, not that the topic is absent. This reads historical evidence, "
        "never workspace files, another Session, or re-executes the original tool."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "effect_id": {"type": "string", "minLength": 1},
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "query": {"type": "string", "minLength": 1},
            "part": {"type": "string", "enum": ["content", "data"]},
            "offset": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": 100},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
            "case_sensitive": {"type": "boolean"},
        },
        "required": ["effect_id", "digest", "query"],
        "additionalProperties": False,
    }

    def __init__(self, sessions: SessionService, *, max_chars: int) -> None:
        self._sessions = sessions
        self._max_chars = max_chars

    async def execute(self, arguments, context) -> ToolOutput:
        if await self._sessions.workspace_for(context.session_id) != context.workspace:
            raise ValueError("tool-output-workspace-mismatch")
        payload = resolve_tool_output(
            await self._sessions.read_session(context.session_id),
            await self._sessions.read_effects(context.session_id),
            session_id=context.session_id,
            effect_id=arguments["effect_id"],
            digest=arguments["digest"],
        )
        return ToolOutput(
            render_output_search(
                payload,
                effect_id=arguments["effect_id"],
                digest=arguments["digest"],
                query=arguments["query"],
                part=arguments.get("part", "content"),
                offset=arguments.get("offset", 0),
                count=arguments.get("count", 10),
                context_lines=arguments.get("context_lines", 0),
                case_sensitive=arguments.get("case_sensitive", True),
                max_chars=self._max_chars,
            )
        )
