"""Pure, bounded expansion of verified M3 provenance into whole-Turn pages.

The only facts are the supplied Session events. This reader has no Store,
writer, request authority or mutable pending state. Its detached snapshot may
resolve an old disclosed replacement even after a wider one hides it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.history import HistoryCursor, HistoryReadPolicy
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import ModelMessage
from traceh.session.protocol import require_session_protocol
from traceh.session.surface_replacement import (
    SURFACE_MESSAGE_TYPES,
    SURFACE_REPLACE,
    SurfaceReplacement,
    SurfaceToolFold,
    parse_surface_replacement,
    surface_conversation,
    surface_message,
)


class HistoryReadError(ValueError):
    """A bounded failure; no source text is carried by the error.

    An oversized Turn occupies one stable page slot. Its successor can be
    disclosed by the request owner without returning any part of that Turn.
    """

    def __init__(self, code: str, *, next_cursor: HistoryCursor | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.next_cursor = next_cursor


@dataclass(frozen=True, slots=True)
class HistoryBlock:
    block_id: str
    version: str
    observed_through_seq: int
    observed_at: str
    summary: str
    first_cursor: HistoryCursor
    _source_refs_json: str

    @property
    def source_refs(self) -> list[dict[str, JsonValue]]:
        return json.loads(self._source_refs_json)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    block_id: str
    body: str
    content_digest: str
    content_bytes: int
    _page_json: str

    @property
    def page(self) -> dict[str, JsonValue]:
        return json.loads(self._page_json)


@dataclass(frozen=True, slots=True)
class _Node:
    seq: int
    sources: tuple[int, ...]
    message_json: str | None
    ref_json: str
    turn_end: int | None


@dataclass(frozen=True, slots=True)
class _Root:
    seq: int
    block: HistoryBlock


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    """Immutable derived values, never a second persistent source of truth."""

    _policy: HistoryReadPolicy
    _nodes: tuple[_Node, ...]
    _roots: tuple[_Root, ...]
    _visible_ids: tuple[str, ...]

    def directory(self) -> tuple[HistoryBlock, ...]:
        return tuple(self.resolve(block_id) for block_id in self._visible_ids)

    def resolve(self, block_id: str) -> HistoryBlock:
        return self._root(block_id).block

    def _root(self, block_id: str) -> _Root:
        for root in self._roots:
            if root.block.block_id == block_id:
                return root
        raise HistoryReadError("history-block-unavailable")

    def leaf_refs(self, block_id: str) -> tuple[dict, ...]:
        return tuple(json.loads(node.ref_json) for node in self._leaves(self._root(block_id)))

    def original_bytes(self, block_id: str) -> int:
        """Full source-message array size; not a raw-page disclosure grant."""
        return _array_bytes(self._leaves(self._root(block_id)))

    def _leaves(self, root):
        nodes = {node.seq: node for node in self._nodes}
        stack = [root.seq]
        visited: set[int] = set()
        leaves: list[_Node] = []
        while stack:
            seq = stack.pop()
            if seq in visited:
                raise HistoryReadError("history-source-invalid")
            visited.add(seq)
            if len(visited) > self._policy.max_source_events:
                raise HistoryReadError("history-source-resource-limit")
            node = nodes[seq]
            if node.message_json is None:
                stack.extend(node.sources)
            else:
                if node.turn_end is None or node.turn_end > root.block.observed_through_seq:
                    raise HistoryReadError("history-source-invalid")
                leaves.append(node)
        # Replacement append order differs from original conversation order.
        leaves.sort(key=lambda node: node.seq)
        return leaves

    def _pages(self, root):
        """One complete-Turn layout shared by reading and keyword navigation."""
        leaves = self._leaves(root)
        groups: list[list[_Node]] = []
        for leaf in leaves:
            if not groups or groups[-1][0].turn_end != leaf.turn_end:
                groups.append([])
            groups[-1].append(leaf)
        pages: list[tuple[tuple[_Node, ...], bool]] = []
        pending: list[_Node] = []
        pending_bytes = 2
        for group in groups:
            _require_tool_pairs(group)
            group_bytes = _array_bytes(group)
            if group_bytes > self._policy.page_bytes or len(group) > self._policy.page_messages:
                if pending:
                    pages.append((tuple(pending), True))
                    pending = []
                    pending_bytes = 2
                pages.append((tuple(group), False))
                continue
            combined_bytes = pending_bytes + group_bytes - 2 + bool(pending)
            if pending and (
                combined_bytes > self._policy.page_bytes
                or len(pending) + len(group) > self._policy.page_messages
            ):
                pages.append((tuple(pending), True))
                pending = []
                combined_bytes = group_bytes
            pending.extend(group)
            pending_bytes = combined_bytes
        if pending:
            pages.append((tuple(pending), True))
        return pages

    def search_records(self):
        """Return detached, source-bound text positions, never disclosure grants."""
        records, seen = [], set()
        for block in self.directory():
            for index, (nodes, readable) in enumerate(self._pages(self._root(block.block_id))):
                for node in nodes:
                    if node.seq in seen:
                        continue
                    seen.add(node.seq)
                    records.append(
                        {
                            "reference": {
                                "block_id": block.block_id,
                                "version": block.version,
                                "leaf_ref": json.loads(node.ref_json),
                                "cursor": HistoryCursor(
                                    block.block_id, self._policy.digest, index
                                ).to_dict(),
                                "readable": readable,
                            },
                            "text": json.loads(node.message_json).get("content") or "",
                        }
                    )
        return tuple(records)

    def read_page(self, *, block_id: str, cursor: HistoryCursor) -> HistoryPage:
        root = self._root(block_id)
        if (
            type(cursor) is not HistoryCursor
            or cursor.block_id != block_id
            or cursor.policy_digest != self._policy.digest
        ):
            raise HistoryReadError("history-cursor-invalid")
        pages = self._pages(root)
        if cursor.index >= len(pages):
            raise HistoryReadError("history-cursor-invalid")
        next_cursor = (
            HistoryCursor(block_id, self._policy.digest, cursor.index + 1)
            if cursor.index + 1 < len(pages)
            else None
        )
        selected, readable = pages[cursor.index]
        if not readable:
            raise HistoryReadError("history-page-resource-limit", next_cursor=next_cursor)
        body = "[" + ",".join(node.message_json for node in selected) + "]"
        payload = {
            "policy_digest": self._policy.digest,
            "index": cursor.index,
            "leaf_refs": [json.loads(node.ref_json) for node in selected],
            "next_cursor": None if next_cursor is None else next_cursor.to_dict(),
        }
        return HistoryPage(
            block_id=block_id,
            body=body,
            content_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            content_bytes=len(body.encode("utf-8")),
            _page_json=canonical_json(payload),
        )


def _array_bytes(nodes: list[_Node]) -> int:
    return 2 + sum(len(node.message_json.encode("utf-8")) for node in nodes) + len(nodes) - 1


def _require_tool_pairs(group: list[_Node]) -> None:
    pending: set[str] = set()
    seen: set[str] = set()
    for node in group:
        message = ModelMessage.from_dict(json.loads(node.message_json))
        for call in message.tool_calls:
            if call.id in seen:
                raise HistoryReadError("history-tool-pair-invalid")
            seen.add(call.id)
            pending.add(call.id)
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise HistoryReadError("history-tool-pair-invalid")
            pending.remove(message.tool_call_id)
    if pending:
        raise HistoryReadError("history-tool-pair-invalid")


def _event_ref(event: EventEnvelope) -> dict[str, JsonValue]:
    return {
        "stream_id": event.stream_id,
        "event_id": str(event.event_id),
        "seq": event.seq,
        "type": event.type,
        "digest": fingerprint(event.to_dict()),
    }


def read_history(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    through_seq: int,
    policy: HistoryReadPolicy,
) -> HistorySnapshot:
    """Validate one exact observed prefix and derive its bounded History graph.

    ``max_source_bytes`` counts canonical whole-envelope bytes, without an
    artificial enclosing array. This cap is checked before M3 graph traversal.
    Neither source identity nor freshness is inferred from paths or wall time.
    """

    if type(through_seq) is not int or through_seq < 1:
        raise HistoryReadError("history-source-boundary-invalid")
    if through_seq > len(events):
        raise HistoryReadError("history-source-unavailable")
    if through_seq > policy.max_source_events:
        raise HistoryReadError("history-source-resource-limit")
    prefix = events[:through_seq]
    require_session_protocol(prefix, session_id=session_id)
    source_bytes = 0
    for number, event in enumerate(prefix, 1):
        if event.seq != number or event.stream_id != f"session:{session_id}":
            raise HistoryReadError("history-source-binding-mismatch")
        source_bytes += len(canonical_json(event.to_dict()).encode("utf-8"))
        if source_bytes > policy.max_source_bytes:
            raise HistoryReadError("history-source-resource-limit")
    # Local import prevents invariants -> Context -> History -> invariants cycles.
    from traceh.session.invariants import check_surface_replacement_sources

    if check_surface_replacement_sources(prefix):
        raise HistoryReadError("history-source-invalid")
    turn_ends = closed_turn_membership(prefix)
    depths: dict[int, int] = {}
    nodes: list[_Node] = []
    roots: list[_Root] = []
    root_ids: dict[int, str] = {}
    for event in prefix:
        if event.type in SURFACE_MESSAGE_TYPES:
            message = canonical_json(surface_message(event).to_dict())
            nodes.append(
                _Node(
                    event.seq,
                    (),
                    message,
                    canonical_json(_event_ref(event)),
                    turn_ends.get(event.seq),
                )
            )
            depths[event.seq] = 0
        elif event.type == SURFACE_REPLACE:
            replacement = parse_surface_replacement(event)
            depth = 1 + max(depths[seq] for seq in replacement.source_seqs)
            if depth > policy.max_depth:
                raise HistoryReadError("history-depth-resource-limit")
            depths[event.seq] = depth
            ref = _event_ref(event)
            if isinstance(replacement, SurfaceToolFold):
                nodes.append(
                    _Node(event.seq, replacement.source_seqs, None, canonical_json(ref), None)
                )
                continue
            cut_event = prefix[replacement.cut_seq - 1]
            block_id = fingerprint({"session_id": session_id, "replacement_ref": ref})
            block = HistoryBlock(
                block_id=block_id,
                version=ref["digest"],
                observed_through_seq=replacement.cut_seq,
                observed_at=cut_event.occurred_at.isoformat(),
                summary=canonical_json(replacement.message.to_dict()),
                first_cursor=HistoryCursor(block_id, policy.digest, 0),
                _source_refs_json=canonical_json([ref, _event_ref(cut_event)]),
            )
            roots.append(_Root(event.seq, block))
            root_ids[event.seq] = block_id
            nodes.append(_Node(event.seq, replacement.source_seqs, None, canonical_json(ref), None))
    visible = tuple(
        root_ids[entry.seq]
        for entry in surface_conversation(prefix)
        if isinstance(entry.replacement, SurfaceReplacement)
    )
    if len(visible) > policy.max_blocks:
        raise HistoryReadError("history-block-resource-limit")
    return HistorySnapshot(policy, tuple(nodes), tuple(roots), visible)


def closed_step_membership(events: tuple[EventEnvelope, ...]) -> dict[int, int]:
    """Map original Surface leaves to the real ``step/end`` that closed them.

    The Turn-level map cannot describe a long single Turn: a task that never
    ends a Turn has no closed history at all by that measure, even after
    hundreds of finished tool groups. A closed Step is the smaller unit that is
    equally safe to fold - its model response is complete, its tool calls have
    all landed, and nothing in it is still running.
    """

    result: dict[int, int] = {}
    open_step: str | None = None
    leaves: list[int] = []
    for event in events:
        if event.type == "step/start":
            identity = event.data.get("step_id")
            if open_step is not None or type(identity) is not str or not identity:
                raise HistoryReadError("history-source-invalid")
            open_step = identity
            leaves = []
        elif event.type == "step/end":
            if open_step is None or event.data.get("step_id") != open_step:
                raise HistoryReadError("history-source-invalid")
            result.update((seq, event.seq) for seq in leaves)
            open_step = None
            leaves = []
        elif event.type in SURFACE_MESSAGE_TYPES and open_step is not None:
            leaves.append(event.seq)
    return result


def closed_turn_membership(events: tuple[EventEnvelope, ...]) -> dict[int, int]:
    """Map original Surface leaves to their real closing Turn, shared by evidence readers."""
    result: dict[int, int] = {}
    open_turn: str | None = None
    leaves: list[int] = []
    for event in events:
        if event.type == "turn/start":
            identity = event.data.get("turn_id")
            if open_turn is not None or type(identity) is not str or not identity:
                raise HistoryReadError("history-source-invalid")
            open_turn = identity
            leaves = []
        elif event.type == "turn/end":
            if open_turn is None or event.data.get("turn_id") != open_turn:
                raise HistoryReadError("history-source-invalid")
            result.update((seq, event.seq) for seq in leaves)
            open_turn = None
            leaves = []
        elif event.type in SURFACE_MESSAGE_TYPES and open_turn is not None:
            leaves.append(event.seq)
    return result
