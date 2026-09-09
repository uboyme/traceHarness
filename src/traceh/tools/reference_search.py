"""Model-requested search uses the same borrowed Session reader as disclosure."""

from copy import deepcopy

from traceh.api.tools import EffectKind, ToolOutput
from traceh.session.reference_search import history_receipt, parse_request


def _configure_limits(tool, policy):
    tool.input_schema = deepcopy(tool.input_schema)
    tool.input_schema["properties"]["limit"].update(
        maximum=policy.max_blocks,
        default=policy.max_blocks,
    )
    tool.description += (
        f" limit must be between 1 and {policy.max_blocks}; omit it to use the configured limit. "
        f"query must fit {policy.max_query_bytes} UTF-8 bytes. "
        "A parameter error is not a no-hit result; "
        "correct the arguments before drawing conclusions."
    )


class HistorySearchTool:
    name = "search_history"
    effect_kind = EffectKind.PURE_READ
    description = (
        "Search this Session's compacted original history using a literal keyword or phrase. "
        "Use a short term; you may try another term when needed. The Tool returns a receipt, "
        "not evidence. The next Step's current reference context contains a bounded search "
        "page with snippets, exact read_action handles and possibly next_cursor. "
        "Read the original page when a snippet is insufficient. A no-hit query is not proof "
        "that the topic never existed. Do not search workspace files for history."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1},
            "cursor": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "source_digest": {"type": "string"},
                            "query_digest": {"type": "string"},
                            "policy_digest": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 1},
                        },
                        "required": ["source_digest", "query_digest", "policy_digest", "offset"],
                        "additionalProperties": False,
                    },
                ],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, read_session, *, policy):
        _configure_limits(self, policy)
        self._read_session = read_session
        self._policy = policy

    async def execute(self, arguments, context):
        parse_request(arguments, self._policy)
        events = await self._read_session(context.session_id)
        receipt = history_receipt(
            events,
            session_id=context.session_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            tool_call_id=context.tool_call_id,
            policy=self._policy,
        )
        return ToolOutput(
            content=(
                "Search accepted. Inspect the next Step's current references for search "
                "results. This receipt contains no evidence."
            ),
            data={"search_receipt": receipt},
        )


class SkillSearchTool:
    name = "search_skill"
    effect_kind = EffectKind.PURE_READ
    input_schema = HistorySearchTool.input_schema
    description = (
        "Search currently selected Skill navigation by a short literal keyword or phrase. "
        "Search covers IDs, titles, summaries and tags, including section and resource/chunk "
        "navigation omitted from automatic references. It does not search undisclosed body text. "
        "Returns a receipt; the next Step's references contain bounded matching snippets and "
        "exact read_action arguments for request_skill_reference. Read the actual section or "
        "chunk before answering questions about its procedure. "
        "If you found only a manual's title/summary, search again with "
        "the question's subject to locate its section or chunk; reading that summary adds no "
        "procedure details. A search handle grants only the exact shown read_action, not a "
        "different tier or guessed chapter. Use next_cursor from the current "
        "search page to continue. This cannot select Skills or enable plugins."
    )

    def __init__(self, read_session, read_selection, *, policy):
        _configure_limits(self, policy)
        self._read_session = read_session
        self._read_selection = read_selection
        self._policy = policy

    async def execute(self, arguments, context):
        from traceh.kernel.composition import CompositionSnapshot
        from traceh.session.reference_search import ReferenceSearchError
        from traceh.session.skill_search import receipt, source_view
        from traceh.session.skill_selection import head_ref

        parse_request(arguments, self._policy)
        events = await self._read_session(context.session_id)
        accepted = receipt(
            events,
            session_id=context.session_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            tool_call_id=context.tool_call_id,
            policy=self._policy,
        )
        snapshots = [
            e
            for e in events
            if e.type == "request/snapshot" and e.data.get("step_id") == context.step_id
        ]
        if len(snapshots) != 1:
            raise ReferenceSearchError("reference-search-binding-mismatch")
        composition = CompositionSnapshot.from_dict(
            events[snapshots[0].data["source_seq"] - 1].data
        )
        selections = await self._read_selection(context.session_id)
        view = source_view(composition, selections, context.session_id, self._policy)
        if view.digest != accepted["source_digest"] or head_ref(
            context.session_id, await self._read_selection(context.session_id)
        ) != head_ref(context.session_id, selections):
            raise ReferenceSearchError("reference-search-source-changed")
        return ToolOutput(
            content=(
                "Search accepted. Inspect the next Step's references; this receipt is not evidence."
            ),
            data={"search_receipt": accepted},
        )


class MemorySearchTool:
    name = "search_memory"
    effect_kind = EffectKind.PURE_READ
    input_schema = HistorySearchTool.input_schema
    description = (
        "Search the current project's active approved Memory facts by a short literal keyword "
        "or phrase. This can discover facts omitted from automatic references. Search covers "
        "memory IDs, fact slots and approved fact text; never proposals, "
        "revoked or replaced facts. "
        "Returns only a receipt. The next Step's current references contain bounded snippets "
        "and exact read_action handles; read complete facts if snippets are insufficient. "
        "Use next_cursor only from a currently admitted search page; try another term if needed. "
        "Does not approve facts or bind a project."
    )

    def __init__(self, read_session, read_memory, recheck_memory, *, policy):
        _configure_limits(self, policy)
        self._read_session = read_session
        self._read_memory = read_memory
        self._recheck_memory = recheck_memory
        self._policy = policy

    async def execute(self, arguments, context):
        from traceh.session.memory_search import receipt, source_view
        from traceh.session.reference_search import ReferenceSearchError

        parse_request(arguments, self._policy)
        events = await self._read_session(context.session_id)
        source = await self._read_memory(context.session_id)
        accepted = receipt(
            events,
            session_id=context.session_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            tool_call_id=context.tool_call_id,
            policy=self._policy,
        )
        view = source_view(source, context.session_id, self._policy)
        if view.digest != accepted["source_digest"] or (
            source is not None and not await self._recheck_memory(source)
        ):
            raise ReferenceSearchError("reference-search-source-changed")
        return ToolOutput(
            content=(
                "Search accepted. Inspect the next Step's current references; "
                "this receipt contains no evidence."
            ),
            data={"search_receipt": accepted},
        )
