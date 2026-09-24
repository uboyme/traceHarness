"""A single long Turn can fold its own finished tool groups.

The existing fold could only cut at a closed Turn, so a task that runs for
hundreds of Steps inside one Turn had no closed history at all by that measure
and carried every tool result into every later request. These tests drive the
real Runtime: one Turn, several finished tool groups, and a water mark that
actually fires.
"""

from __future__ import annotations

import json

import pytest

from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.compaction import CompactionPolicy
from traceh.session.event_store import InMemoryEventStore
from traceh.session.surface_replacement import (
    FOLD_STEP,
    SURFACE_REPLACE,
    parse_surface_replacement,
)

LINE = "a line of source that is long enough to matter for token counting"
BODY = (LINE + "\n") * 40


def policy(**overrides) -> TokenBudgetPolicy:
    """Room for the whole task, so folding is a choice rather than a rescue."""

    return TokenBudgetPolicy(
        **{
            "encoding": "cl100k_base",
            "window_tokens": 20000,
            "output_reserve_tokens": 256,
            "safety_margin_tokens": 64,
            "trigger_percent": 15,
            "fold_relief_percent": 8,
            "fold_protect_recent_groups": 1,
            **overrides,
        }
    )


def tight_policy(**overrides) -> TokenBudgetPolicy:
    """A window the same task cannot fit in unfolded."""

    return policy(
        **{
            "window_tokens": 6000,
            "trigger_percent": 40,
            "fold_relief_percent": 25,
            **overrides,
        }
    )


class Reader:
    """Reads the same file each Step, then answers. One Turn throughout."""

    name = "scripted"

    def __init__(self, reads: int) -> None:
        self.reads = reads
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) <= self.reads:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(f"c{len(self.requests)}", "read_file", {"path": "source.txt"}),
                ),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="done reading", completion=CompletionCategory.NORMAL)


async def run(tmp_path, *, reads: int, token_budget: TokenBudgetPolicy | None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text(BODY, encoding="utf-8")
    provider = Reader(reads)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=reads + 2,
            max_output_tokens=256,
            token_budget=token_budget,
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        result = await runtime.run_existing(session_id, "read the source repeatedly")
        events = await runtime.sessions.read_session(session_id)
        violations = await runtime.check_invariants(session_id)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    finally:
        await runtime.dispose()
    return result, events, provider, violations, replay


def folds(events):
    return [
        parse_surface_replacement(e)
        for e in events
        if e.type == SURFACE_REPLACE and e.data.get("method") == "tool-fold"
    ]


@pytest.mark.asyncio
async def test_one_turn_folds_its_own_finished_tool_groups(tmp_path) -> None:
    result, events, provider, violations, replay = await run(
        tmp_path, reads=6, token_budget=tight_policy()
    )

    assert result.reason == "completed"
    folded = folds(events)
    assert folded, "the water mark never produced a fold inside the single Turn"
    # Every cut is a Step boundary, counted in tool groups rather than Turns.
    assert {f.boundary.unit for f in folded} == {FOLD_STEP}
    assert {f.boundary.kept_recent for f in folded} == {1}
    # There is exactly one Turn: the old contract had nothing to cut at.
    assert len([e for e in events if e.type == "turn/end"]) == 1
    assert not violations and not replay


@pytest.mark.asyncio
async def test_folding_actually_removes_the_old_text_from_later_requests(tmp_path) -> None:
    """The point is the next request, not the event: the body must stop repeating."""

    def body_copies(request) -> int:
        # The tool serializes its result as JSON, so the stored newlines are
        # escaped; match one whole line of the source instead of the raw block.
        return sum(LINE in (message.content or "") for message in request.messages)

    # Same task, same window, same six reads. Only the water marks differ.
    _, without_events, without, _, _ = await run(
        tmp_path / "off",
        reads=6,
        token_budget=policy(fold_relief_percent=None, fold_protect_recent_groups=None),
    )
    _, with_events, with_folding, violations, replay = await run(
        tmp_path / "on", reads=6, token_budget=policy()
    )

    assert not folds(without_events)
    assert folds(with_events)
    unfolded = body_copies(without.requests[-1])
    folded = body_copies(with_folding.requests[-1])
    assert unfolded == 6, f"the baseline should carry every body: {unfolded}"
    assert folded < unfolded, f"the folded bodies were still being resent: {folded}"
    # Folding replaces the body with a pointer, so the reference survives where
    # the text does not: the model can still find what was folded.
    assert any(
        '"read_tool_output"' in (m.content or "") for m in with_folding.requests[-1].messages
    )
    assert not violations and not replay


@pytest.mark.asyncio
async def test_the_most_recent_group_is_never_folded(tmp_path) -> None:
    """The model must still be able to read what it just asked for."""

    _, events, _, violations, _ = await run(tmp_path, reads=6, token_budget=tight_policy())
    folded_sources = {seq for fold in folds(events) for seq in fold.source_seqs}
    results = [e.seq for e in events if e.type == "tool/result"]

    assert results[-1] not in folded_sources
    assert not violations


@pytest.mark.asyncio
async def test_without_water_marks_the_same_task_is_refused_at_the_hard_limit(tmp_path) -> None:
    """Absence disables folding; it never picks marks on its own.

    The same six reads then run the history past the hard input limit, and the
    request is refused rather than quietly trimmed. That contrast is the point
    of the feature: with marks configured this task completes, without them it
    stops honestly.
    """

    from traceh.llm.token_meter import RequestTokenBudgetExceeded

    with pytest.raises(RequestTokenBudgetExceeded):
        await run(
            tmp_path,
            reads=6,
            token_budget=tight_policy(
                fold_relief_percent=None, fold_protect_recent_groups=None
            ),
        )


def test_half_a_water_mark_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="token-fold-watermarks-incomplete"):
        policy(fold_protect_recent_groups=None)
    with pytest.raises(ValueError, match="token-fold-watermarks-incomplete"):
        policy(fold_relief_percent=None)


def test_relief_must_sit_below_the_trigger() -> None:
    """A relief mark at or above the trigger would fold without ever relieving."""

    with pytest.raises(ValueError, match="token-fold-relief-percent-invalid"):
        policy(trigger_percent=15, fold_relief_percent=15)


@pytest.mark.asyncio
async def test_a_failed_admission_fold_stops_the_request(tmp_path, monkeypatch) -> None:
    """Turn-front maintenance may fail and continue; this may not.

    Folding here is the reason the request is small enough to send. Recording
    the failure and carrying on would publish exactly the oversized request the
    water mark exists to prevent, so the Step stops before the Provider.
    """

    from traceh.session.compaction import CompactionError, CompactionService

    async def refuse(self, session_id, **kwargs):
        raise CompactionError("compaction-write-failed", committed=False)

    monkeypatch.setattr(CompactionService, "fold_closed_steps", refuse)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text(BODY, encoding="utf-8")
    provider = Reader(6)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=8,
            max_output_tokens=256,
            token_budget=tight_policy(),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        with pytest.raises(CompactionError):
            await runtime.run_existing(session_id, "read the source repeatedly")
        events = await runtime.sessions.read_session(session_id)
        calls_before_failure = len(provider.requests)
    finally:
        await runtime.dispose()

    failures = [
        e.data
        for e in events
        if e.type == "surface/compaction-failed" and e.data.get("method") == "step-fold"
    ]
    assert failures and failures[-1]["code"] == "compaction-write-failed"
    # The Provider was not asked again after the fold refused.
    assert len(provider.requests) == calls_before_failure


@pytest.mark.asyncio
async def test_a_partly_folded_group_stays_a_well_formed_conversation(tmp_path) -> None:
    """One replacement folds one result, so a group is foldable in pieces.

    Every intermediate state is therefore a real state the model could be sent,
    and each one must keep its calls and results paired in order.
    """

    _, events, provider, violations, replay = await run(
        tmp_path, reads=6, token_budget=tight_policy()
    )

    assert len(folds(events)) > 1, "no partial states existed to check"
    for request in provider.requests:
        pending: list[str] = []
        for message in request.messages:
            if message.role == "assistant":
                pending = [call.id for call in message.tool_calls]
            elif message.role == "tool":
                assert message.tool_call_id in pending, "a folded result lost its call"
                pending.remove(message.tool_call_id)
        assert not pending, "a call was left without its result"
    assert not violations and not replay


def test_step_membership_tolerates_an_interrupted_then_resumed_step() -> None:
    """A crash closes the open Step before a new one starts, and folding relies on that.

    If it did not, a resumed Session would raise while selecting fold candidates,
    and because admission-path folding stops the request on error, the Session
    could become unable to send anything at all. The recovery service and the
    loop's interrupt path both append the closing ``step/end`` first; this pins
    the assumption rather than leaving it implicit.
    """

    from uuid import uuid4

    from traceh.api.events import EventEnvelope
    from traceh.session.history import closed_step_membership

    def event(seq: int, kind: str, **data) -> EventEnvelope:
        return EventEnvelope(
            event_id=uuid4(),
            stream_id="session:s",
            seq=seq,
            type=kind,
            data={"turn_id": "t", **data},
            occurred_at="2026-09-21T00:00:00Z",
            schema_version=1,
        )

    events = (
        event(1, "step/start", step_id="a"),
        event(2, "tool/result", step_id="a", tool_name="read_file", status="succeeded"),
        # The crash: recovery closes "a" before anything else may open.
        event(3, "step/end", step_id="a", reason="interrupted"),
        event(4, "step/start", step_id="b"),
        event(5, "tool/result", step_id="b", tool_name="read_file", status="succeeded"),
        event(6, "step/end", step_id="b", reason="model_response"),
    )

    membership = closed_step_membership(events)
    # Both results belong to their own closing Step, so both remain foldable.
    assert membership == {2: 3, 5: 6}


@pytest.mark.asyncio
async def test_a_folded_placeholder_hands_back_a_runnable_read_action(tmp_path) -> None:
    """Reopening must be at least as easy as re-running, or folding loses money.

    The assistant call that produced a folded result stays visible right above
    the placeholder, so re-running costs the model a copy of arguments it can
    already read. A real trial showed what happens when reopening costs more
    than that: the investigator re-ran the original tool 22 times, every one of
    them after a fold, while the read-back tool succeeded on all nine occasions
    it was actually used. The placeholder therefore carries the call, not just
    the reference.
    """

    _, events, _, violations, _ = await run(tmp_path, reads=6, token_budget=tight_policy())
    folded = folds(events)
    assert folded

    shown = json.loads(folded[0].message.content)
    action = shown["read"]
    assert action["tool"] == "read_tool_output"
    # Pre-filled from this result's own reference: nothing left to assemble.
    # The identity lives once, inside the runnable call. Repeating it beside the
    # action is what made 67 placeholders cost 19,766 tokens in a real trial.
    assert "output_ref" not in shown
    assert action["arguments"]["effect_id"] and action["arguments"]["digest"]
    # Exactly the reader's required arguments and nothing else: the placeholder
    # is itself the cost of folding, so repeating values that already default
    # to "the whole content" would pay bytes for nothing.
    from traceh.tools.output import ReadToolOutput

    assert set(action["arguments"]) == set(ReadToolOutput.input_schema["required"])
    assert not violations


@pytest.mark.asyncio
async def test_the_folded_read_action_actually_resolves_the_original_bytes(tmp_path) -> None:
    """A pre-filled action that does not work would be worse than none."""

    from traceh.session.tool_output import resolve_tool_output

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text(BODY, encoding="utf-8")
    provider = Reader(6)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=8,
            max_output_tokens=256,
            token_budget=tight_policy(),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read the source repeatedly")
        events = await runtime.sessions.read_session(session_id)
        effects = await runtime.sessions.read_effects(session_id)
    finally:
        await runtime.dispose()

    folded = folds(events)
    assert folded
    action = json.loads(folded[0].message.content)["read"]["arguments"]
    payload = resolve_tool_output(
        events,
        effects,
        session_id=session_id,
        effect_id=action["effect_id"],
        digest=action["digest"],
    )
    # The exact bytes the fold collapsed, reachable by following its own action.
    assert LINE in payload["content"]


class ReopeningReader:
    """Reads, then follows a folded placeholder's own read_action, then keeps going.

    This is the shape the real trial produced: the model loses an earlier read
    to a fold, reopens it through the reader, and then continues working long
    enough that the reopened result becomes fold-eligible in its turn.
    """

    name = "scripted"

    def __init__(self, reads: int, tail: int) -> None:
        self.reads, self.tail = reads, tail
        self.requests: list = []
        self.reopened = False

    def _folded_action(self, request):
        for message in request.messages:
            if message.role != "tool":
                continue
            try:
                shown = json.loads(message.content or "")
            except ValueError:
                continue
            if isinstance(shown, dict) and "read" in shown:
                return shown["read"]
        return None

    async def complete(self, request):
        self.requests.append(request)
        turn = len(self.requests)
        if turn <= self.reads:
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"r{turn}", "read_file", {"path": "source.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        action = self._folded_action(request)
        if action is not None and not self.reopened:
            self.reopened = True
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"back{turn}", action["tool"], action["arguments"]),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        if turn <= self.reads + self.tail:
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"t{turn}", "read_file", {"path": "source.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="done", completion=CompletionCategory.NORMAL)


async def run_with_reopen(tmp_path, *, budget: int):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text(BODY, encoding="utf-8")
    provider = ReopeningReader(reads=5, tail=8)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=20,
            max_output_tokens=256,
            token_budget=policy(fold_protect_readback_utf8_bytes=budget),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read, reopen, keep reading")
        events = await runtime.sessions.read_session(session_id)
        violations = await runtime.check_invariants(session_id)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    finally:
        await runtime.dispose()
    return provider, events, violations, replay


@pytest.mark.asyncio
async def test_reopened_evidence_is_protected_up_to_its_byte_budget(tmp_path) -> None:
    """A generous budget keeps reopened pages; the model does not lose them again."""

    from traceh.session.tool_output import OUTPUT_TOOL_IDS

    provider, events, violations, replay = await run_with_reopen(tmp_path, budget=200_000)
    assert provider.reopened, "the model never followed a folded read action"

    reopened = {
        e.seq
        for e in events
        if e.type == "tool/result" and e.data.get("tool_name") in OUTPUT_TOOL_IDS
    }
    folded_sources = {seq for fold in folds(events) for seq in fold.source_seqs}
    assert folded_sources, "nothing folded, so nothing was proven"
    assert not folded_sources & reopened
    assert not violations and not replay


@pytest.mark.asyncio
async def test_without_a_budget_a_reopened_page_folds_back_to_its_original_address(
    tmp_path,
) -> None:
    """Aged-out reopened pages are ordinary candidates, and point at the origin.

    The previous attempt exempted them forever, which only moved the failure:
    28 permanently retained pages were 42.5% of the request that then hit the
    ceiling. They may age out - but the placeholder must send the model to the
    page that read-back already named, not to the read-back's own output. The
    latter nests: reopening it returns a page envelope wrapping the previous
    page envelope, one more layer every round.
    """

    from traceh.session.tool_output import OUTPUT_TOOL_IDS

    provider, events, violations, replay = await run_with_reopen(tmp_path, budget=0)
    assert provider.reopened

    by_seq = {e.seq: e for e in events}
    calls = {
        e.data["tool_call_id"]: e.data.get("arguments") or {}
        for e in events
        if e.type == "tool/call"
    }
    folded_reopened = [
        fold
        for fold in folds(events)
        for seq in fold.source_seqs
        if by_seq[seq].data.get("tool_name") in OUTPUT_TOOL_IDS
    ]
    assert folded_reopened, "a reopened page should age out when nothing protects it"

    for fold in folded_reopened:
        source = by_seq[fold.source_seqs[0]]
        origin = calls[source.data["tool_call_id"]]
        action = json.loads(fold.message.content)["read"]["arguments"]
        # The same call the model already made - idempotent, and one level.
        assert action["effect_id"] == origin["effect_id"]
        assert action["digest"] == origin["digest"]
        # Never the read-back's own output, which is what nests.
        assert action["effect_id"] != source.data["output_ref"]["effect_id"]
    assert not violations and not replay


WANTED = "the paragraph the investigation keeps coming back to, again and again"
PASSING = "a line it reads once and never needs a second time"
WANTED_BODY = (WANTED + "\n") * 40
PASSING_BODY = (PASSING + "\n") * 30


class DemandingReader:
    """Reopens one page twice and a second page once, ending on the second.

    This is the shape a real trial produced once re-running the original tool
    had been cured: 31 of 60 reader calls were exact repeats, five pages opened
    four times each. The model was not confused - it kept needing the same
    paragraphs, and a purely recent protection kept handing them back and then
    taking them away again.

    The script ends on the page it wants *least*, so recency and demand
    disagree about which one the budget should hold. It tells them apart by the
    ``chars`` the placeholder itself reports, which is what a real model sees.
    """

    name = "scripted"

    def __init__(self, reads: int, tail: int) -> None:
        self.reads, self.tail = reads, tail
        self.requests: list = []
        # The wanted file is the larger one, so the placeholder reporting the
        # most characters is the page this reader keeps needing.
        self.plan = [max, max, min]
        self.reopened: list = []

    def _folded_actions(self, request) -> dict[int, dict]:
        shown: dict[int, dict] = {}
        for message in request.messages:
            if message.role != "tool":
                continue
            try:
                placeholder = json.loads(message.content or "")
            except ValueError:
                continue
            if isinstance(placeholder, dict) and "read" in placeholder:
                shown[placeholder["chars"]] = placeholder["read"]
        return shown

    async def complete(self, request):
        self.requests.append(request)
        turn = len(self.requests)
        if turn <= self.reads:
            path = "wanted.txt" if turn % 2 else "passing.txt"
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"r{turn}", "read_file", {"path": path}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        available = self._folded_actions(request)
        if available and len(self.reopened) < len(self.plan):
            choose = self.plan[len(self.reopened)]
            action = available[choose(available)]
            if action is not None:
                self.reopened.append(choose)
                return ModelResponse(
                    content="",
                    tool_calls=(ToolCall(f"back{turn}", action["tool"], action["arguments"]),),
                    completion=CompletionCategory.TOOL_HANDOFF,
                )
        if turn <= self.reads + self.tail:
            # Filler names a new file each time: re-running a folded read is
            # itself demand now, and this script is about read-back pages.
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"t{turn}", "read_file", {"path": f"filler{turn}.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="done", completion=CompletionCategory.NORMAL)


@pytest.mark.asyncio
async def test_the_budget_holds_the_page_the_model_keeps_asking_for(tmp_path) -> None:
    """Demand decides who keeps the capacity; recency only breaks ties.

    Two pages are reopened, one twice and one once, and the budget fits a
    single reopened result. Ordering by recency would hold the page asked for
    least, because it happens to be the newest, and the page the model actually
    depends on would be folded and fetched all over again. That is the thrash
    this ordering exists to stop, so the surviving page must be the wanted one.
    """

    from traceh.session.tool_output import OUTPUT_TOOL_IDS

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "wanted.txt").write_text(WANTED_BODY, encoding="utf-8")
    (workspace / "passing.txt").write_text(PASSING_BODY, encoding="utf-8")
    for index in range(40):
        # Sized between the two pages, so the script's max/min choice among
        # placeholders still lands on the wanted and the passing page.
        (workspace / f"filler{index}.txt").write_text(BODY, encoding="utf-8")
    provider = DemandingReader(reads=6, tail=10)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=30,
            max_output_tokens=256,
            # Measured: a reopened wanted page is 3844 bytes and the passing
            # one 2495, so this holds exactly one wanted page and nothing else.
            # Recency would spend it on the passing page instead, which is what
            # makes the two orderings distinguishable here.
            token_budget=policy(fold_protect_readback_utf8_bytes=4800),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read both, reopen what you need")
        events = await runtime.sessions.read_session(session_id)
        violations = await runtime.check_invariants(session_id)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    finally:
        await runtime.dispose()

    assert provider.reopened == provider.plan, "the model never carried out the planned reopens"

    calls = {
        e.data["tool_call_id"]: e.data.get("arguments") or {}
        for e in events
        if e.type == "tool/call"
    }
    # Which stored page belongs to which file, from the original reads.
    origin_path = {
        e.data["output_ref"]["effect_id"]: calls[e.data["tool_call_id"]].get("path")
        for e in events
        if e.type == "tool/result"
        and "output_ref" in e.data
        and e.data.get("tool_name") not in OUTPUT_TOOL_IDS
    }
    reopens = {
        e.seq: origin_path.get(calls[e.data["tool_call_id"]].get("effect_id"))
        for e in events
        if e.type == "tool/result" and e.data.get("tool_name") in OUTPUT_TOOL_IDS
    }
    assert sorted(reopens.values()) == ["passing.txt", "wanted.txt", "wanted.txt"]

    folded = {seq for fold in folds(events) for seq in fold.source_seqs}
    survivors = {path for seq, path in reopens.items() if seq not in folded}
    assert folded & set(reopens), "no reopened page aged out, so the budget proved nothing"
    assert survivors == {"wanted.txt"}, (
        "the capacity went to the page the model asked for least"
    )
    assert not violations and not replay


class RerunningReader:
    """Loses a read to a fold, then re-runs the original read instead of reading back.

    This is what a real model did in a Multi run at a 48k window: the fold
    placeholder offered a read action, the read-back tool was never used, and
    the coordinator re-ran ``read_file`` on the same path - 11 and 4 times in two
    runs. Every other read here names a new file, so only one invocation is ever
    re-run and its fate is the whole question.
    """

    name = "scripted"

    def __init__(self, reads: int, tail: int) -> None:
        self.reads, self.tail = reads, tail
        self.requests: list = []
        self.reran = 0

    def _first_read_folded(self, request) -> bool:
        for message in request.messages:
            if message.role == "tool" and message.tool_call_id == "r1":
                return '"read"' in (message.content or "")
        return False

    async def complete(self, request):
        self.requests.append(request)
        turn = len(self.requests)
        if turn <= self.reads:
            path = "a.txt" if turn == 1 else f"b{turn}.txt"
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"r{turn}", "read_file", {"path": path}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        if not self.reran and self._first_read_folded(request):
            self.reran = turn
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"again{turn}", "read_file", {"path": "a.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        if turn <= self.reads + self.tail:
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"t{turn}", "read_file", {"path": f"c{turn}.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="done", completion=CompletionCategory.NORMAL)


async def run_with_rerun(tmp_path, *, budget: int):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    for index in range(40):
        for prefix in ("b", "c"):
            (workspace / f"{prefix}{index}.txt").write_text(BODY, encoding="utf-8")
    (workspace / "a.txt").write_text(WANTED_BODY, encoding="utf-8")
    provider = RerunningReader(reads=5, tail=10)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=25,
            max_output_tokens=256,
            token_budget=policy(fold_protect_readback_utf8_bytes=budget),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read, lose one to a fold, keep reading")
        events = await runtime.sessions.read_session(session_id)
        violations = await runtime.check_invariants(session_id)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    finally:
        await runtime.dispose()
    rerun_results = [
        e.seq
        for e in events
        if e.type == "tool/result" and str(e.data.get("tool_call_id")).startswith("again")
    ]
    folded = {seq for fold in folds(events) for seq in fold.source_seqs}
    return provider, rerun_results, folded, violations, replay


@pytest.mark.asyncio
async def test_a_rerun_after_a_fold_is_held_like_a_read_back(tmp_path) -> None:
    """The re-run copy stays; without it the model folds it away and fetches it again.

    The placeholder asks for a read-back, but a model that re-runs the original
    read is expressing the same demand. Treating that copy as an ordinary
    candidate folded it two Steps later in the real run, and the repair loop
    spent its remaining time fetching the same files over and over.
    """

    provider, rerun, folded, violations, replay = await run_with_rerun(tmp_path, budget=40_000)
    assert provider.reran, "the fold never reached the first read, so nothing was re-run"
    assert len(rerun) == 1
    assert folded, "nothing folded, so nothing was proven"
    assert rerun[0] not in folded, "the re-run copy was folded again"
    assert not violations and not replay


@pytest.mark.asyncio
async def test_without_a_budget_the_rerun_copy_is_an_ordinary_candidate(tmp_path) -> None:
    """The same script with no capacity folds the re-run copy: the budget is what holds it."""

    provider, rerun, folded, violations, replay = await run_with_rerun(tmp_path, budget=0)
    assert provider.reran
    assert rerun and rerun[0] in folded
    assert not violations and not replay


class RepeatingReader:
    """Reads one file twice while it is still visible, then only new files.

    Repeating a call before anything was folded is ordinary work - a test run
    repeated after an edit looks exactly like this - so neither copy has been
    lost and neither may claim the capacity meant for lost evidence.
    """

    name = "scripted"

    def __init__(self, tail: int) -> None:
        self.tail = tail
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        turn = len(self.requests)
        if turn <= 2 + self.tail:
            path = "x.txt" if turn <= 2 else f"c{turn}.txt"
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"r{turn}", "read_file", {"path": path}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="done", completion=CompletionCategory.NORMAL)


@pytest.mark.asyncio
async def test_repeating_a_still_visible_call_is_not_demand(tmp_path) -> None:
    """Only a re-run *after* a fold is demand; an ordinary repeat still folds."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "x.txt").write_text(WANTED_BODY, encoding="utf-8")
    for index in range(40):
        (workspace / f"c{index}.txt").write_text(BODY, encoding="utf-8")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=20,
            max_output_tokens=256,
            token_budget=policy(fold_protect_readback_utf8_bytes=200_000),
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=RepeatingReader(tail=10),
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read x twice, then move on")
        events = await runtime.sessions.read_session(session_id)
        violations = await runtime.check_invariants(session_id)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    finally:
        await runtime.dispose()

    repeated = [
        e.seq
        for e in events
        if e.type == "tool/result" and e.data.get("tool_call_id") in {"r1", "r2"}
    ]
    folded = {seq for fold in folds(events) for seq in fold.source_seqs}
    assert len(repeated) == 2
    assert set(repeated) <= folded, "a copy repeated while visible was held as if it were lost"
    assert not violations and not replay
