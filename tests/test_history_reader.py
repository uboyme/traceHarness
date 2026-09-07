"""Exact History pages derive only from the real Session/M3 provenance path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.history import (
    HISTORY_PAGE_POLICY_VERSION,
    HistoryCursor,
    HistoryPageRequest,
    HistoryReadPolicy,
)
from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ToolCall
from traceh.session.compaction import CompactionService
from traceh.session.event_store import InMemoryEventStore
from traceh.session.history import HistoryReadError, read_history
from traceh.session.protocol import CONTEXT_PROTOCOL, SessionProtocolError
from traceh.session.service import SessionService
from traceh.session.surface_replacement import (
    SURFACE_MESSAGE_TYPES,
    parse_surface_replacement,
    surface_message,
    surface_prefix,
    surface_replacement_data,
)


def _policy(**overrides):
    # All bounds are explicit fixture choices, never production defaults.
    return HistoryReadPolicy(
        **(
            dict(
                max_blocks=8,
                max_depth=8,
                page_bytes=12_000,
                page_messages=32,
                max_source_events=512,
                max_source_bytes=800_000,
                max_requests=4,
            )
            | overrides
        )
    )


async def _turn(sessions, session_id, *, identity, text, tools=False):
    before = await sessions.read_session(session_id)
    step_id = f"{identity}-step"
    common = {"turn_id": identity, "step_id": step_id}
    await sessions.append_session(session_id, "turn/start", {"turn_id": identity})
    await sessions.append_session(session_id, "step/start", common)
    await sessions.append_session(session_id, "user/message", common | {"content": text})
    if tools:
        calls = tuple(
            ToolCall(id=f"{identity}-{index}", name="inspect", arguments={"n": index})
            for index in range(2)
        )
        await sessions.append_session(
            session_id,
            "assistant/message",
            common
            | {
                "content": "Reading recorded evidence",
                "tool_calls": [call.to_dict() for call in calls],
            },
        )
        for call in calls:
            await sessions.append_session(
                session_id,
                "tool/call",
                {
                    "step_id": step_id,
                    "tool_call_id": call.id,
                    "audit": "audit-only-call",
                },
            )
            await sessions.append_session(
                session_id,
                "tool/result",
                {
                    "step_id": step_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "content": f"Observed {call.id}",
                    "data": {"private_audit": "audit-only-result-data"},
                },
            )
    await sessions.append_session(
        session_id,
        "assistant/message",
        common
        | {
            "content": f"Answer to {text}",
            "tool_calls": [],
        },
    )
    await sessions.append_session(session_id, "step/end", common)
    end = await sessions.append_session(session_id, "turn/end", {"turn_id": identity})
    events = await sessions.read_session(session_id)
    leaves = tuple(event for event in events[len(before) :] if event.type in SURFACE_MESSAGE_TYPES)
    return end, leaves


async def _fixture(
    tmp_path, *, texts=("heat transfer", "控制律约束", "spectral residue"), nested=True, tools=False
):
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    ends, groups = [], []
    for index, text in enumerate(texts):
        end, leaves = await _turn(
            sessions, session_id, identity=f"turn-{index}", text=text, tools=tools and index == 1
        )
        ends.append(end)
        groups.append(leaves)
    compaction = CompactionService(sessions)
    narrow = None
    if nested:
        narrow = await compaction.replace_through(
            session_id, through_seq=ends[0].seq, summary="Earlier exchange"
        )
    wide = await compaction.replace_through(
        session_id, through_seq=ends[-1].seq, summary="Recorded exchanges"
    )
    events = await sessions.read_session(session_id)
    return sessions, session_id, events, tuple(groups), narrow, wide


def _read(events, session_id, **overrides):
    return read_history(
        events, session_id=session_id, through_seq=len(events), policy=_policy(**overrides)
    )


def _body(leaves):
    return canonical_json([surface_message(event).to_dict() for event in leaves])


def _ref(event):
    return {
        "stream_id": event.stream_id,
        "event_id": str(event.event_id),
        "seq": event.seq,
        "type": event.type,
        "digest": fingerprint(event.to_dict()),
    }


def test_policy_and_request_round_trip_are_strict_and_detached():
    policy = _policy()
    assert HistoryReadPolicy.from_dict(policy.to_dict()) == policy
    assert policy.digest == fingerprint(
        {"version": HISTORY_PAGE_POLICY_VERSION, "config": policy.to_dict()}
    )
    cursor = HistoryCursor("a" * 64, policy.digest, 0)
    for tier in ("section", "chunk"):
        request = HistoryPageRequest(cursor.block_id, cursor, tier)
        encoded = request.to_dict()
        assert HistoryPageRequest.from_dict(encoded) == request
        encoded["cursor"]["index"] = 42
        assert request.cursor.index == 0
    with pytest.raises(FrozenInstanceError):
        cursor.index = 1


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "2", None])
def test_policy_rejects_nonpositive_or_coerced_limits(value):
    for field in _policy().to_dict():
        with pytest.raises(ValueError, match="history-policy-invalid"):
            _policy(**{field: value})


@pytest.mark.parametrize(
    "change", ["extra", "missing", "negative", "bool", "digest", "block", "tier", "request-extra"]
)
def test_exact_request_parser_rejects_forged_cursor_shapes(change):
    request = HistoryPageRequest("a" * 64, HistoryCursor("a" * 64, "b" * 64, 0), "section")
    raw = request.to_dict()
    if change == "extra":
        raw["cursor"]["through_seq"] = 1
    elif change == "missing":
        del raw["cursor"]["index"]
    elif change == "negative":
        raw["cursor"]["index"] = -1
    elif change == "bool":
        raw["cursor"]["index"] = False
    elif change == "digest":
        raw["cursor"]["policy_digest"] = "B" * 64
    elif change == "block":
        raw["cursor"]["block_id"] = "c" * 64
    elif change == "tier":
        raw["requested_tier"] = "summary"
    else:
        raw["source"] = "user"
    with pytest.raises(ValueError, match="history-(cursor|request)-invalid"):
        HistoryPageRequest.from_dict(raw)


@pytest.mark.parametrize("nested", [False, True])
async def test_pages_restore_original_order_and_exact_envelope_sources(tmp_path, nested):
    _, session_id, events, groups, narrow, wide = await _fixture(tmp_path, nested=nested)
    snapshot = _read(events, session_id)
    (block,) = snapshot.directory()
    replacement = events[wide.replacement_seq - 1]
    cut = events[parse_surface_replacement(replacement).cut_seq - 1]
    assert block.block_id == fingerprint(
        {"session_id": session_id, "replacement_ref": _ref(replacement)}
    )
    assert block.version == _ref(replacement)["digest"]
    assert block.source_refs == [_ref(replacement), _ref(cut)]
    assert block.observed_through_seq == cut.seq
    assert block.observed_at == cut.occurred_at.isoformat()
    assert block.summary == canonical_json(parse_surface_replacement(replacement).message.to_dict())
    page = snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor)
    leaves = tuple(event for group in groups for event in group)
    assert page.body == _body(leaves)
    assert page.content_digest == hashlib.sha256(page.body.encode("utf-8")).hexdigest()
    assert page.content_bytes == len(page.body.encode("utf-8"))
    assert page.page == {
        "policy_digest": _policy().digest,
        "index": 0,
        "leaf_refs": [_ref(event) for event in leaves],
        "next_cursor": None,
    }
    if narrow is not None:
        # Real late compaction makes source wire order differ from logical order.
        assert narrow.replacement_seq > groups[1][0].seq
        narrow_event = events[narrow.replacement_seq - 1]
        narrow_id = fingerprint({"session_id": session_id, "replacement_ref": _ref(narrow_event)})
        old = snapshot.resolve(narrow_id)
        assert old not in snapshot.directory()
        assert snapshot.read_page(block_id=old.block_id, cursor=old.first_cursor).body == _body(
            groups[0]
        )


async def test_snapshot_remains_exact_after_input_and_output_graph_mutation(tmp_path):
    _, session_id, events, groups, _, _ = await _fixture(tmp_path)
    snapshot = _read(events, session_id)
    block = snapshot.directory()[0]
    page = snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor)
    original = page.body
    events[groups[0][0].seq - 1].data["content"] = "caller changed input"
    block.source_refs[0]["digest"] = "f" * 64
    page.page["leaf_refs"][0]["digest"] = "f" * 64
    assert snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor).body == original
    assert block.source_refs[0]["digest"] == block.version


async def test_pages_keep_whole_turns_and_all_tool_pairs(tmp_path):
    _, session_id, events, groups, _, _ = await _fixture(tmp_path, tools=True)
    snapshot = _read(events, session_id, page_messages=len(groups[1]))
    block = snapshot.directory()[0]
    cursor = block.first_cursor
    pages = []
    while cursor is not None:
        page = snapshot.read_page(block_id=block.block_id, cursor=cursor)
        pages.append(page)
        raw = page.page["next_cursor"]
        cursor = None if raw is None else HistoryCursor.from_dict(raw)
    assert [page.body for page in pages] == [_body(group) for group in groups]
    tool_messages = json.loads(pages[1].body)
    calls = tool_messages[1]["tool_calls"]
    results = [item for item in tool_messages if item["role"] == "tool"]
    assert [item["id"] for item in calls] == [item["tool_call_id"] for item in results]
    assert "audit-only" not in pages[1].body


async def test_page_byte_limit_counts_unicode_escaping_and_array_punctuation(tmp_path):
    _, session_id, events, groups, _, _ = await _fixture(
        tmp_path, texts=('路径 "约束" \\ branch',), nested=False
    )
    exact = len(_body(groups[0]).encode("utf-8"))
    snapshot = _read(events, session_id, page_bytes=exact)
    block = snapshot.directory()[0]
    assert (
        snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor).content_bytes
        == exact
    )
    limited = _read(events, session_id, page_bytes=exact - 1)
    limited_block = limited.directory()[0]
    with pytest.raises(HistoryReadError, match="history-page-resource-limit"):
        limited.read_page(block_id=limited_block.block_id, cursor=limited_block.first_cursor)


async def test_oversized_turn_has_stable_rejected_slot_without_partial_prose(tmp_path):
    _, session_id, events, groups, _, _ = await _fixture(
        tmp_path, texts=("oversized-record-" * 200, "later exchange"), nested=False
    )
    snapshot = _read(events, session_id, page_bytes=len(_body(groups[1]).encode("utf-8")))
    block = snapshot.directory()[0]
    with pytest.raises(HistoryReadError) as caught:
        snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor)
    assert caught.value.code == "history-page-resource-limit"
    assert "oversized-record" not in str(caught.value)
    assert caught.value.next_cursor == HistoryCursor(
        block.block_id, _policy(page_bytes=len(_body(groups[1]).encode("utf-8"))).digest, 1
    )
    later = snapshot.read_page(block_id=block.block_id, cursor=caught.value.next_cursor)
    assert later.page["index"] == 1
    assert later.body == _body(groups[1])
    assert later.page["next_cursor"] is None


async def test_cursor_and_old_page_stay_exact_after_later_unrelated_events(tmp_path):
    sessions, session_id, events, _, _, _ = await _fixture(tmp_path)
    before = _read(events, session_id)
    block = before.directory()[0]
    page = before.read_page(block_id=block.block_id, cursor=block.first_cursor)
    await _turn(sessions, session_id, identity="later-turn", text="unrelated later exchange")
    later = await sessions.read_session(session_id)
    exact = read_history(later, session_id=session_id, through_seq=len(events), policy=_policy())
    after = _read(later, session_id)
    assert exact == before
    assert after.resolve(block.block_id) == block
    assert after.read_page(block_id=block.block_id, cursor=block.first_cursor) == page


@pytest.mark.parametrize("change", ["block", "policy", "index"])
async def test_reader_rejects_cursor_not_bound_to_exact_page(tmp_path, change):
    _, session_id, events, _, _, _ = await _fixture(tmp_path)
    snapshot = _read(events, session_id)
    block = snapshot.directory()[0]
    cursor = block.first_cursor
    if change == "block":
        cursor = replace(cursor, block_id="a" * 64)
    elif change == "policy":
        cursor = replace(cursor, policy_digest=_policy(page_messages=1).digest)
    else:
        cursor = replace(cursor, index=200)
    with pytest.raises(HistoryReadError, match="history-cursor-invalid"):
        snapshot.read_page(block_id=block.block_id, cursor=cursor)


@pytest.mark.parametrize("limit", ["max_source_events", "max_source_bytes", "max_depth"])
async def test_explicit_graph_caps_fail_without_partial_directory(tmp_path, limit):
    _, session_id, events, _, _, _ = await _fixture(tmp_path)
    maximum = {
        "max_source_events": len(events),
        "max_source_bytes": sum(
            len(canonical_json(event.to_dict()).encode("utf-8")) for event in events
        ),
        "max_depth": 2,
    }[limit]
    assert _read(events, session_id, **{limit: maximum}).directory()
    with pytest.raises(HistoryReadError, match="history-(source|depth)-resource-limit"):
        _read(events, session_id, **{limit: maximum - 1})


@pytest.mark.parametrize("change", ["body", "digest", "cut", "future", "audit", "format"])
async def test_reader_reuses_m3_rejection_of_invalid_provenance(tmp_path, change):
    _, session_id, events, groups, _, wide = await _fixture(tmp_path, nested=False)
    replacement = events[wide.replacement_seq - 1]
    if change == "body":
        events[groups[0][0].seq - 1].data["content"] = "forged original"
    elif change == "digest":
        replacement.data["source_digest"] = "a" * 64
    elif change == "cut":
        replacement.data["cut_seq"] = groups[-1][-1].seq
    elif change == "future":
        replacement.data["source_seqs"][-1] = replacement.seq + 1
    elif change == "audit":
        replacement.data["source_seqs"].append(replacement.seq - 1)
    else:
        replacement.data["format_version"] = 1
    with pytest.raises(HistoryReadError, match="history-source-invalid"):
        _read(events, session_id)


async def test_closed_turn_with_missing_tool_result_cannot_be_disclosed(tmp_path):
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    common = {"turn_id": "incomplete-turn", "step_id": "incomplete-step"}
    for event_type, data in (
        ("turn/start", {"turn_id": common["turn_id"]}),
        ("step/start", common),
        ("user/message", common | {"content": "observe state"}),
        (
            "assistant/message",
            common
            | {
                "content": "",
                "tool_calls": [
                    ToolCall(id="unfinished-call", name="inspect", arguments={}).to_dict()
                ],
            },
        ),
        ("step/end", common),
        ("turn/end", {"turn_id": common["turn_id"]}),
    ):
        await sessions.append_session(session_id, event_type, data)
    # This is an intentional corrupt closed prefix at the storage boundary.
    # The existing M3 source rule does not itself require a missing result.
    prior = await sessions.read_session(session_id)
    prefix = surface_prefix(prior, cut_seq=prior[-1].seq)
    payload = surface_replacement_data(
        method="manual",
        cut_seq=prefix.cut_seq,
        source_seqs=prefix.source_seqs,
        source_digest=prefix.source_digest,
        source_utf8_bytes=prefix.source_utf8_bytes,
        history_utf8_bytes=prefix.history_utf8_bytes,
        kept_recent_turns=0,
        policy_digest=None,
        summarizer=None,
        summary="Unfinished historical operation",
        summary_truncated=False,
    )
    event = EventEnvelope.materialize(
        prior[0].stream_id, prior[-1].seq + 1, PendingEvent("surface/replace", payload)
    )
    snapshot = _read((*prior, event), session_id)
    block = snapshot.directory()[0]
    with pytest.raises(HistoryReadError, match="history-tool-pair-invalid"):
        snapshot.read_page(block_id=block.block_id, cursor=block.first_cursor)


async def test_old_session_foreign_stream_and_missing_boundary_are_rejected(tmp_path):
    _, session_id, events, _, _, _ = await _fixture(tmp_path)
    assert events[0].data["context_protocol"] == CONTEXT_PROTOCOL
    with pytest.raises(SessionProtocolError):
        _read(events, "another-session")
    with pytest.raises(HistoryReadError, match="history-source-binding-mismatch"):
        _read((events[0], replace(events[1], stream_id="session:another"), *events[2:]), session_id)
    with pytest.raises(HistoryReadError, match="history-source-unavailable"):
        read_history(events, session_id=session_id, through_seq=len(events) + 1, policy=_policy())
    events[0].data["context_protocol"] = CONTEXT_PROTOCOL - 1
    with pytest.raises(SessionProtocolError):
        _read(events, session_id)
