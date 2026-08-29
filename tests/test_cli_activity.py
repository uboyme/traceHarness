"""Liveness, interrupt UX and event-number explanation for `traceh chat`.

Time is injected rather than waited for: a test that needs to observe a 10-second
heartbeat advances a manual clock instead of sleeping, so the suite stays fast
and the assertions are about behaviour rather than timing luck.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import ModelRequest, ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.chat.activity import (
    ActivityTracker,
    Clock,
    default_clock,
)
from traceh.chat.driver import (
    ChatDriver,
    SessionEventUpdate,
    TurnInterruptedUpdate,
)
from traceh.cli.activity import (
    DEFAULT_HEARTBEAT_SECONDS,
    render_activity_wait,
    validate_heartbeat_seconds,
)
from traceh.cli.chat import (
    INTERRUPTED_EXIT_CODE,
    INTERRUPTED_TURN_NOTICE,
    run_chat,
)
from traceh.cli.console import Console
from traceh.cli.errors import CliConfigurationError
from traceh.cli.main import build_parser
from traceh.cli.timeline import TimelineRenderer
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.projections import StateProjector

TIMELINE_TASKS = ("traceh-chat-driver-events", "traceh-chat-driver-heartbeat")


class ManualClock:
    """A clock the test drives. No real waiting anywhere.

    ``sleep`` records a *deadline* and only wakes once the clock reaches it, and
    ``advance`` walks the clock forward through those deadlines in order. Honouring
    the requested duration is what makes this a clock rather than a barrier: an
    earlier version simply released every sleeper on any ``advance``, which meant a
    heartbeat asking for 0.1s and one asking for 10s were indistinguishable - and
    that is precisely what hid the phase bug this fixture now exercises.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Event]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        waiter = asyncio.Event()
        self._sleepers.append((self.now + max(0.0, seconds), waiter))
        await waiter.wait()

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking each sleeper exactly at its own deadline."""

        target = self.now + seconds
        while True:
            pending = [deadline for deadline, _ in self._sleepers if deadline <= target]
            if not pending:
                break
            # Step to the earliest deadline so a sleeper that re-arms with a
            # shorter delay is still served in the right order.
            self.now = min(pending)
            ready = [waiter for deadline, waiter in self._sleepers if deadline <= self.now]
            self._sleepers = [
                entry for entry in self._sleepers if entry[0] > self.now
            ]
            for waiter in ready:
                waiter.set()
            await self.settle()
        self.now = target
        await self.settle()

    @staticmethod
    async def settle() -> None:
        """Let woken tasks run to their next await point."""

        for _ in range(10):
            await asyncio.sleep(0)

    @property
    def clock(self) -> Clock:
        return Clock(monotonic=self.monotonic, sleep=self.sleep)


class FakeConsole:
    def __init__(self, inputs: tuple[object, ...] = ()) -> None:
        self._inputs = list(inputs)
        self.lines: list[str] = []

    def read_line(self, prompt: str) -> str:
        if not self._inputs:
            raise EOFError
        item = self._inputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)

    def write(self, text: str) -> None:
        self.lines.append(text)

    @property
    def console(self) -> Console:
        return Console(read_line=self.read_line, write=self.write)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)

    def index_of(self, needle: str) -> int:
        for position, line in enumerate(self.lines):
            if needle in line:
                return position
        raise AssertionError(f"{needle!r} not found in:\n{self.output}")

    def waiting_lines(self) -> list[str]:
        return [line for line in self.lines if line.startswith("[waiting ")]

    def event_lines(self) -> list[str]:
        return [line for line in self.lines if line.startswith("[event ")]

    def shown_seqs(self) -> list[int]:
        return [
            int(line.split("]")[0].removeprefix("[event ").strip())
            for line in self.event_lines()
        ]


class GatedProvider:
    """Blocks inside the model call until released, once per call."""

    name = "scripted"

    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.started = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if self.calls == 1:
                self.first_cancelled.set()
            raise
        self.started.clear()
        self.release = asyncio.Event()
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


class GateTool:
    name = "gate_tool"
    description = "Blocks until released."
    input_schema: dict = {"type": "object", "properties": {}}
    effect_kind = EffectKind.WORKSPACE_READ

    def __init__(self, name: str = "gate_tool") -> None:
        self.name = name
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> ToolOutput:
        self.entered.set()
        await self.release.wait()
        return ToolOutput(content="gate opened")


class SecretArgTool:
    """A read tool whose arguments must never reach a heartbeat line."""

    name = "read_file"
    description = "Reads."
    input_schema: dict = {"type": "object", "properties": {"path": {"type": "string"}}}
    effect_kind = EffectKind.WORKSPACE_READ

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> ToolOutput:
        self.entered.set()
        await self.release.wait()
        return ToolOutput(content="body")


def build_runtime(
    tmp_path: Path,
    provider,
    *,
    tools=(),
    data_dir: Path | None = None,
    include_default_tools: bool = True,
):
    return build_default_runtime(
        RuntimeConfig(
            data_dir=data_dir or (tmp_path / ".traceh"),
            provider="scripted",
            model="qwen-plus",
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
        additional_tools=tools,
        include_default_tools=include_default_tools,
    )


def envelope(event_type: str, data: dict, *, seq: int = 1) -> EventEnvelope:
    return EventEnvelope.materialize("session:x", seq, PendingEvent(event_type, data))


def live_display_tasks() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in TIMELINE_TASKS and not task.done()
    ]


# -- the fixture itself --------------------------------------------------


async def test_the_manual_clock_honours_requested_deadlines() -> None:
    """The clock is load-bearing, so its own contract is asserted.

    A fixture that woke every sleeper on any ``advance`` would make a 0.1s wait
    and a 10s wait indistinguishable, which is how a phase bug can pass a suite
    that looks thorough. Sleepers must therefore wake at their own deadline and
    in deadline order.
    """

    clock = ManualClock()
    woken: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woken.append(f"{name}@{clock.now:g}")

    tasks = [
        asyncio.create_task(sleeper("late", 10.0)),
        asyncio.create_task(sleeper("early", 2.0)),
    ]
    await ManualClock.settle()

    await clock.advance(1.0)
    assert woken == [], "nobody is due at t=1"

    await clock.advance(1.0)
    assert woken == ["early@2"], "only the 2s sleeper is due at t=2"

    await clock.advance(10.0)
    assert woken == ["early@2", "late@10"], "the 10s sleeper wakes at its own deadline"
    assert clock.now == 12.0
    await asyncio.gather(*tasks)


# -- tracker unit --------------------------------------------------------


def _waiting_lines(tracker: ActivityTracker) -> tuple[str, ...]:
    return tuple(render_activity_wait(update) for update in tracker.due_waits())


def test_tracker_reports_each_crossed_interval_once() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(
        envelope("model/attempt-start", {"attempt_id": "a1", "provider": "p", "model": "m"})
    )

    assert tracker.due_waits() == ()
    clock.now = 9.9
    assert tracker.due_waits() == ()
    clock.now = 10.0
    assert _waiting_lines(tracker) == ("[waiting 10s] Model p/m is still working",)
    assert tracker.due_waits() == ()  # not repeated for the same threshold
    clock.now = 20.5
    assert _waiting_lines(tracker) == ("[waiting 20s] Model p/m is still working",)


def test_tracker_reports_elapsed_time_when_an_activity_ends() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(envelope("model/attempt-start", {"attempt_id": "a1"}))
    clock.now = 23.4

    ended = tracker.observe(envelope("model/attempt-end", {"attempt_id": "a1"}))
    assert ended is not None
    assert ended.elapsed_seconds == pytest.approx(23.4)
    assert tracker.pending_count() == 0
    assert tracker.due_waits() == ()


def test_tracker_keeps_parallel_tools_apart() -> None:
    """`ToolRuntime` runs read tools concurrently; one slot would lose all but one."""

    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(envelope("tool/admitted", {"tool_call_id": "c1", "tool_name": "read_file"}))
    tracker.observe(envelope("tool/admitted", {"tool_call_id": "c2", "tool_name": "search_text"}))
    assert tracker.pending_count() == 2

    clock.now = 10.0
    lines = _waiting_lines(tracker)
    assert len(lines) == 2
    assert any("read_file (call c1)" in line for line in lines)
    assert any("search_text (call c2)" in line for line in lines)
    # Honest wording: a gathered group hides per-tool completion, so the line
    # claims only that no result has been persisted yet.
    assert all("has not reported completion" in line for line in lines)

    tracker.observe(envelope("tool/result", {"tool_call_id": "c1", "tool_name": "read_file"}))
    clock.now = 20.0
    remaining = _waiting_lines(tracker)
    assert remaining == (
        "[waiting 20s] Tool search_text (call c2) has not reported completion",
    )


def test_tracker_ignores_events_without_a_usable_identity() -> None:
    """An unidentifiable activity could never be closed, so it is never opened."""

    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(envelope("model/attempt-start", {"provider": "p", "model": "m"}))
    tracker.observe(envelope("tool/admitted", {"tool_name": "read_file"}))
    tracker.observe(envelope("model/attempt-start", {"attempt_id": 17}))

    assert tracker.pending_count() == 0
    clock.now = 60.0
    assert tracker.due_waits() == ()


def test_tracker_never_shows_tool_arguments() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(
        envelope(
            "tool/admitted",
            {
                "tool_call_id": "c1",
                "tool_name": "shell",
                # NOT a real credential: a key-shaped test fixture.
                "arguments": {"command": "deploy --key sk-proj-FAKE0000FIXTURE"},
                "path": "/etc/secrets.txt",
            },
        )
    )
    clock.now = 10.0
    line = _waiting_lines(tracker)[0]

    assert line == "[waiting 10s] Tool shell (call c1) has not reported completion"
    for fragment in ("deploy", "--key", "sk-proj", "FAKE", "secrets.txt"):
        assert fragment not in line


def test_tracker_sanitizes_hostile_labels() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(
        envelope(
            "tool/admitted",
            {"tool_call_id": "c\n1", "tool_name": "evil\n[waiting 99s] fake\x1b[2J"},
        )
    )
    clock.now = 10.0
    line = _waiting_lines(tracker)[0]

    # Same guarantee - and same residual - as the timeline: the newline and ESC
    # are neutralised so no extra row or control sequence can be produced, while
    # the injected bracket text survives as inert content of this one row.
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert line.startswith("[waiting 10s] Tool "), "the real waited time must lead the row"
    assert line.endswith("has not reported completion")


def test_next_wait_is_scheduled_from_the_activity_not_the_tracker() -> None:
    """The delay must be measured from when the activity started.

    A heartbeat that ticks on its own fixed schedule is phase-locked to the turn,
    not to the work: with a 10s interval and an activity starting at t=10.1, a
    t=20 tick sees 9.9s and stays silent, so the first notice lands at t=30.
    """

    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    assert tracker.seconds_until_next_wait() is None  # nothing in flight

    clock.now = 10.1
    tracker.observe(envelope("tool/admitted", {"tool_call_id": "c1", "tool_name": "shell"}))
    assert tracker.seconds_until_next_wait() == pytest.approx(10.0)

    clock.now = 20.0
    assert tracker.seconds_until_next_wait() == pytest.approx(0.1)
    assert tracker.due_waits() == ()  # not due yet - 9.9s elapsed

    clock.now = 20.1
    assert tracker.seconds_until_next_wait() == pytest.approx(0.0)
    assert _waiting_lines(tracker) == (
        "[waiting 10s] Tool shell (call c1) has not reported completion",
    )
    # The next deadline moves on by one interval rather than staying due.
    assert tracker.seconds_until_next_wait() == pytest.approx(10.0)


def test_next_wait_tracks_the_earliest_of_several_activities() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=10.0, monotonic=clock.monotonic)
    tracker.observe(envelope("tool/admitted", {"tool_call_id": "c1", "tool_name": "a"}))
    clock.now = 4.0
    tracker.observe(envelope("tool/admitted", {"tool_call_id": "c2", "tool_name": "b"}))

    # c1 is due at 10, c2 at 14; the earliest wins.
    assert tracker.seconds_until_next_wait() == pytest.approx(6.0)


def test_a_disabled_tracker_produces_nothing() -> None:
    clock = ManualClock()
    tracker = ActivityTracker(interval_seconds=0.0, monotonic=clock.monotonic)
    tracker.observe(envelope("model/attempt-start", {"attempt_id": "a1"}))
    clock.now = 1000.0

    assert not tracker.enabled
    assert tracker.due_waits() == ()


def test_renderer_appends_a_duration_only_when_given_one() -> None:
    renderer = TimelineRenderer()
    event = envelope("model/attempt-end", {"status": "succeeded"}, seq=11)

    assert renderer.render(event) == "[event 11] Model responded"
    assert renderer.render(event, elapsed_seconds=23.44) == "[event 11] Model responded (23.4s)"


# -- configuration ------------------------------------------------------


def test_heartbeat_interval_defaults_and_can_be_disabled() -> None:
    assert validate_heartbeat_seconds(DEFAULT_HEARTBEAT_SECONDS) == DEFAULT_HEARTBEAT_SECONDS
    assert validate_heartbeat_seconds(0) == 0.0
    assert validate_heartbeat_seconds(2.5) == 2.5


def test_no_timeline_also_disables_the_heartbeat() -> None:
    """A liveness line with no timeline around it would be context-free noise."""

    assert validate_heartbeat_seconds(10.0, timeline=False) == 0.0


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (-1.0, "negative"),
        (-0.5, "negative"),
        (float("nan"), "NaN"),
        (float("inf"), "finite"),
        (float("-inf"), "finite"),
    ],
)
def test_unusable_heartbeat_intervals_are_rejected(value: float, match: str) -> None:
    with pytest.raises(CliConfigurationError, match=match):
        validate_heartbeat_seconds(value)


def test_parser_exposes_the_heartbeat_flag() -> None:
    parser = build_parser()
    assert parser.parse_args(["chat", "."]).heartbeat_seconds == DEFAULT_HEARTBEAT_SECONDS
    assert parser.parse_args(["chat", ".", "--heartbeat-seconds", "3"]).heartbeat_seconds == 3.0
    assert parser.parse_args(["chat", ".", "--heartbeat-seconds", "0"]).heartbeat_seconds == 0.0


def test_the_default_clock_is_monotonic_not_wall_clock() -> None:
    """Wall clock would jump or reverse if the system time were adjusted mid-turn."""

    import time

    clock = default_clock()
    assert clock.monotonic is time.monotonic
    assert clock.sleep is asyncio.sleep


# -- heartbeat through the chat -----------------------------------------


async def test_a_slow_model_reports_that_it_is_still_working(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="Finally."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("a slow question",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)

    await clock.advance(10.0)
    assert console.waiting_lines() == ["[waiting 10s] Model scripted/qwen-plus is still working"]
    await clock.advance(10.0)
    assert console.waiting_lines() == [
        "[waiting 10s] Model scripted/qwen-plus is still working",
        "[waiting 20s] Model scripted/qwen-plus is still working",
    ]
    # The turn is demonstrably still running while those lines appear.
    assert not console.has("assistant>")

    provider.release.set()
    await asyncio.wait_for(chat, timeout=10)

    assert console.has("Model responded (20.0s)")
    assert console.has("assistant> Finally.")
    # No further heartbeat after the activity closed, however much time passes.
    before = list(console.waiting_lines())
    await clock.advance(100.0)
    assert console.waiting_lines() == before
    assert not live_display_tasks()


async def test_a_slow_tool_reports_without_leaking_its_arguments(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("secret file body", encoding="utf-8")
    tool = SecretArgTool()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="", tool_calls=(ToolCall("c1", "read_file", {"path": "notes.txt"}),)
            ),
            ModelResponse(content="Read it."),
        )
    )
    # Replaces the built-in so the name stays on the timeline's path allowlist:
    # the timeline may therefore show the path, which makes "the heartbeat must
    # not" a real assertion rather than a vacuous one.
    runtime = build_runtime(tmp_path, provider, tools=(tool,), include_default_tools=False)
    console = FakeConsole(("read notes",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(tool.entered.wait(), timeout=10)

    await clock.advance(10.0)
    assert console.waiting_lines() == [
        "[waiting 10s] Tool read_file (call c1) has not reported completion"
    ]
    for fragment in ("notes.txt", "secret file body"):
        assert fragment not in "\n".join(console.waiting_lines())
    # The timeline did show the path, so the heartbeat's silence is a real choice.
    assert console.has("Tool read_file requested notes.txt")

    tool.release.set()
    await asyncio.wait_for(chat, timeout=10)

    assert console.has("Tool read_file succeeded (10.0s)")
    assert not live_display_tasks()


async def test_a_tool_starting_off_phase_is_reported_one_interval_later(
    tmp_path: Path,
) -> None:
    """End to end: an activity that starts mid-interval must not wait two.

    The model call is held open so the turn has already been running for a while
    when the tool starts, putting the tool deliberately out of phase with the
    display's own start. Before the fix the first notice appeared only at the next
    multiple of the interval - nearly two intervals into the wait.
    """

    tool = GateTool()
    provider = GatedProvider(
        (ModelResponse(content="", tool_calls=(ToolCall("c1", "gate_tool", {}),)),)
    )
    runtime = build_runtime(tmp_path, provider, tools=(tool,))
    console = FakeConsole(("use the gate",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)

    # Let the turn run 10.1s inside the model call, so the tool starts off-phase.
    await clock.advance(10.1)
    assert console.waiting_lines() == ["[waiting 10s] Model scripted/qwen-plus is still working"]
    provider.release.set()
    await asyncio.wait_for(tool.entered.wait(), timeout=10)
    await ManualClock.settle()
    tool_started_at = clock.now

    # 9.9s after the tool started: still nothing about the tool.
    await clock.advance(9.9)
    assert not any("gate_tool" in line for line in console.waiting_lines())

    # One interval after the tool itself started - not after the display's tick.
    await clock.advance(0.2)
    tool_lines = [line for line in console.waiting_lines() if "gate_tool" in line]
    assert tool_lines == [
        "[waiting 10s] Tool gate_tool (call c1) has not reported completion"
    ], f"first notice arrived {clock.now - tool_started_at:.1f}s after the tool started"
    assert clock.now - tool_started_at < 20.0, "must not wait a second interval"

    tool.release.set()
    await asyncio.wait_for(chat, timeout=10)
    assert not live_display_tasks()


async def test_two_parallel_tools_are_tracked_independently(tmp_path: Path) -> None:
    first = GateTool("gate_one")
    second = GateTool("gate_two")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall("c1", "gate_one", {}),
                    ToolCall("c2", "gate_two", {}),
                ),
            ),
            ModelResponse(content="Both done."),
        )
    )
    runtime = build_runtime(tmp_path, provider, tools=(first, second))
    console = FakeConsole(("run both",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(first.entered.wait(), timeout=10)
    await asyncio.wait_for(second.entered.wait(), timeout=10)

    await clock.advance(10.0)
    at_ten = console.waiting_lines()
    assert len(at_ten) == 2, "both concurrent tools must report, not just one"
    assert any("gate_one (call c1)" in line for line in at_ten)
    assert any("gate_two (call c2)" in line for line in at_ten)

    # Each is tracked from its own start, so both cross the next threshold too.
    await clock.advance(10.0)
    at_twenty = [line for line in console.waiting_lines() if "20s" in line]
    assert len(at_twenty) == 2
    assert {"gate_one (call c1)", "gate_two (call c2)"} == {
        fragment
        for fragment in ("gate_one (call c1)", "gate_two (call c2)")
        if any(fragment in line for line in at_twenty)
    }

    # `ToolRuntime` gathers a parallel-safe batch and appends every `tool/result`
    # only once the whole batch is done, so "one finishes while the other keeps
    # reporting" is not observable through real events. That half of the contract
    # is driven directly in `test_tracker_keeps_parallel_tools_apart`.
    first.release.set()
    second.release.set()
    await asyncio.wait_for(chat, timeout=10)

    assert console.has("Tool gate_one succeeded")
    assert console.has("Tool gate_two succeeded")
    assert console.has("assistant> Both done.")
    assert not live_display_tasks()


async def test_a_fast_turn_prints_no_waiting_lines(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="Instant."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("quick",))
    clock = ManualClock()

    await run_chat(
        runtime, console.console, workspace=tmp_path,
        heartbeat_seconds=10.0, clock=clock.clock,
    )

    assert console.waiting_lines() == []
    assert console.has("assistant> Instant.")


async def test_zero_disables_the_heartbeat_but_keeps_the_timeline(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="Done."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("slow",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=0.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    await clock.advance(60.0)

    assert console.waiting_lines() == []
    assert console.has("Model scripted/qwen-plus called")  # timeline still on

    provider.release.set()
    await asyncio.wait_for(chat, timeout=10)
    assert console.has("assistant> Done.")
    assert not live_display_tasks()


async def test_no_timeline_silences_timeline_heartbeat_and_seq_note(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="Quiet."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("slow",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path, timeline=False,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    await clock.advance(60.0)

    assert console.waiting_lines() == []
    assert console.event_lines() == []
    assert not console.has("Timeline shows selected persisted events.")

    provider.release.set()
    await asyncio.wait_for(chat, timeout=10)
    assert console.has("assistant> Quiet.")
    assert console.has("[reason=completed")
    assert not live_display_tasks()


async def test_the_heartbeat_adds_no_events(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="Done."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("slow",))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    session_id = (await runtime.sessions.list_sessions())[0]
    before = len(await runtime.sessions.read_session(session_id))

    for _ in range(5):
        await clock.advance(10.0)
    assert len(console.waiting_lines()) == 5
    # Five heartbeat lines, zero new events.
    assert len(await runtime.sessions.read_session(session_id)) == before

    provider.release.set()
    await asyncio.wait_for(chat, timeout=10)

    events = await runtime.sessions.read_session(session_id)
    assert not any("heartbeat" in event.type or "waiting" in event.type for event in events)
    effects = await runtime.sessions.read_effects(session_id)
    assert not CoreInvariantChecker().check(events, effects)


async def test_a_failing_console_does_not_change_the_turn_result(tmp_path: Path) -> None:
    """A display fault is not a turn outcome."""

    provider = ScriptedLlmProvider((ModelResponse(content="Survived."),))
    runtime = build_runtime(tmp_path, provider)
    clock = ManualClock()
    written: list[str] = []

    def exploding_write(text: str) -> None:
        written.append(text)
        if text.startswith("[event "):
            raise RuntimeError("console exploded")

    inputs = ["one"]

    def read_line(prompt: str) -> str:
        if not inputs:
            raise EOFError
        return inputs.pop(0)

    code = await run_chat(
        runtime,
        Console(read_line=read_line, write=exploding_write),
        workspace=tmp_path,
        heartbeat_seconds=10.0,
        clock=clock.clock,
    )

    assert code == 0
    assert any(line.startswith("assistant> Survived.") for line in written)
    assert not live_display_tasks()


async def test_a_failed_turn_leaves_no_heartbeat_task(tmp_path: Path) -> None:
    class Exploding:
        name = "scripted"

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("provider exploded")

    runtime = build_runtime(tmp_path, Exploding())
    console = FakeConsole(("go",))
    clock = ManualClock()

    await run_chat(
        runtime, console.console, workspace=tmp_path,
        heartbeat_seconds=10.0, clock=clock.clock,
    )

    assert console.has("error: ProviderFailure: provider-failure-unclassified")
    assert not live_display_tasks()


# -- Ctrl+C lifecycle ---------------------------------------------------


async def test_interrupting_a_model_call_shows_the_cancellation_lifecycle(
    tmp_path: Path,
) -> None:
    """The core fix: cancellation events are appended *during* runtime.cancel().

    Tearing the subscription down before cancelling - the old order - published
    every one of them to nobody, so the user saw an abrupt stop with no
    explanation.
    """

    provider = GatedProvider((ModelResponse(content="never delivered"),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("a slow question", "a second question", "/exit"))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    session_id = (await runtime.sessions.list_sessions())[0]
    stream = runtime.sessions.session_stream(session_id)

    # The subscription is still open at the moment of cancellation - that is what
    # lets the lifecycle be observed at all.
    assert runtime.events.subscriber_count(stream) == 1

    chat.cancel()  # exactly how Ctrl+C arrives under asyncio.run on 3.11+
    await asyncio.wait_for(provider.first_cancelled.wait(), timeout=10)
    provider.release.set()
    assert await asyncio.wait_for(chat, timeout=10) == 0

    assert console.has("Cancellation requested")
    assert console.has("Model attempt cancelled")
    assert console.has("Step 1 ended (cancelled)")
    assert console.has("Turn ended (cancelled)")
    assert console.has(INTERRUPTED_TURN_NOTICE)
    # All of it before the notice, which is the readable ordering.
    for line in ("Cancellation requested", "Model attempt cancelled", "Turn ended (cancelled)"):
        assert console.index_of(line) < console.index_of(INTERRUPTED_TURN_NOTICE)

    events = await runtime.sessions.read_session(session_id)
    effects = await runtime.sessions.read_effects(session_id)
    assert not CoreInvariantChecker().check(events, effects)
    projection = StateProjector().project(events)
    assert projection.open_turn_id is None
    assert projection.open_step_id is None

    # The session survived and the next input became a real new turn.
    assert [event.type for event in events].count("turn/start") == 2
    assert console.has("assistant> never delivered")
    assert runtime.events.subscriber_count(stream) == 0
    assert not live_display_tasks()


async def test_interrupting_a_tool_converges_it_before_the_prompt_returns(
    tmp_path: Path,
) -> None:
    tool = GateTool()
    provider = GatedProvider(
        (ModelResponse(content="", tool_calls=(ToolCall("c1", "gate_tool", {}),)),)
    )
    runtime = build_runtime(tmp_path, provider, tools=(tool,))
    console = FakeConsole(("use the gate", "/exit"))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    provider.release.set()
    await asyncio.wait_for(tool.entered.wait(), timeout=10)

    chat.cancel()
    tool.release.set()
    assert await asyncio.wait_for(chat, timeout=10) == 0

    assert console.has(INTERRUPTED_TURN_NOTICE)
    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    assert not CoreInvariantChecker().check(
        events, await runtime.sessions.read_effects(session_id)
    )
    # Every tool call still received a result, synthesized on the cancel path.
    types = [event.type for event in events]
    assert types.count("tool/call") == types.count("tool/result")
    assert not live_display_tasks()


class StubRuntimeForConvergence:
    """A runtime whose cancel() blocks until the test allows it to finish.

    Real components converge too quickly to observe the "second interrupt cannot
    release the caller" property, so convergence itself is gated here.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def cancel(self, session_id: str, *, reason: str = "") -> bool:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return True


async def test_repeated_interrupts_cannot_shorten_convergence() -> None:
    stub = StubRuntimeForConvergence()
    async def consume(_update) -> None:
        return None

    driver = ChatDriver(
        stub,  # type: ignore[arg-type]
        "s1",
        sink=consume,
        timeline=False,
        heartbeat_seconds=0.0,
        clock=Clock(monotonic=lambda: 0.0, sleep=asyncio.sleep),
    )
    task = asyncio.create_task(driver.cancel(reason="test interrupt"))
    await asyncio.wait_for(stub.entered.wait(), timeout=5)

    for attempt in range(3):
        task.cancel()
        for _ in range(6):
            await asyncio.sleep(0)
        assert not task.done(), f"convergence released early on interrupt #{attempt + 1}"

    stub.release.set()
    await asyncio.wait_for(task, timeout=5)

    assert stub.calls == 1, "cancel must not be re-issued per interrupt"


async def test_driver_progress_and_cancel_need_no_stdin_adapter(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="unused"),))
    runtime = build_runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path)
    updates = []
    progress = asyncio.Event()

    async def consume(update) -> None:
        updates.append(update)
        if isinstance(update, SessionEventUpdate) and update.event.type == "model/attempt-start":
            progress.set()

    driver = ChatDriver(
        runtime,
        session_id,
        sink=consume,
        timeline=True,
        heartbeat_seconds=0.0,
        clock=Clock(monotonic=lambda: 0.0, sleep=asyncio.sleep),
    )
    running = asyncio.create_task(driver.run_turn("work without an input loop"))
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    await asyncio.wait_for(progress.wait(), timeout=5)

    await driver.aclose()
    outcome = await asyncio.wait_for(running, timeout=5)
    assert outcome.interrupted
    assert any(isinstance(update, TurnInterruptedUpdate) for update in updates)
    assert any(
        isinstance(update, SessionEventUpdate)
        and update.event.type == "runtime/cancel-requested"
        for update in updates
    )
    await runtime.dispose()


async def test_a_second_interrupt_leaves_the_chat_after_converging(tmp_path: Path) -> None:
    provider = GatedProvider((ModelResponse(content="never delivered"),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("a slow question", "never reached"))
    clock = ManualClock()

    chat = asyncio.create_task(
        run_chat(
            runtime, console.console, workspace=tmp_path,
            heartbeat_seconds=10.0, clock=clock.clock,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=10)

    chat.cancel()
    await asyncio.sleep(0)
    chat.cancel()  # user insists while the first is still converging
    provider.release.set()

    try:
        code = await asyncio.wait_for(chat, timeout=10)
        assert code == INTERRUPTED_EXIT_CODE
    except asyncio.CancelledError:
        pass  # leaving via cancellation is equally acceptable

    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    assert not CoreInvariantChecker().check(
        events, await runtime.sessions.read_effects(session_id)
    )
    # Nothing left running, whichever way it left.
    assert not live_display_tasks()
    assert runtime.events.subscriber_count(runtime.sessions.session_stream(session_id)) == 0


async def test_an_idle_interrupt_leaves_with_resume_information(tmp_path: Path) -> None:
    data_dir = tmp_path / "my data dir"
    provider = ScriptedLlmProvider((ModelResponse(content="hi"),), repeat_last=True)
    runtime = build_runtime(tmp_path, provider, data_dir=data_dir)
    console = FakeConsole((KeyboardInterrupt(),))

    code = await run_chat(runtime, console.console, workspace=tmp_path)

    assert code == INTERRUPTED_EXIT_CODE
    session_id = (await runtime.sessions.list_sessions())[0]
    # Shell-specific quoting is asserted in tests/test_cli_resume.py; here the
    # point is that leaving while idle still shows how to get back in.
    assert console.has("resume later")
    assert console.has(session_id)
    assert console.has(str(data_dir.resolve()))
    assert console.has("traceh sessions")


# -- event numbering ---------------------------------------------------


async def test_the_first_visible_event_keeps_its_real_seq(tmp_path: Path) -> None:
    """seq 1-3 are persisted but hidden, so 4 is correct and must not be renumbered."""

    provider = ScriptedLlmProvider((ModelResponse(content="Done."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("go",))

    await run_chat(runtime, console.console, workspace=tmp_path, heartbeat_seconds=0.0)

    assert console.shown_seqs()[0] == 4
    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    hidden = [event.type for event in events if event.seq < 4]
    assert hidden == ["session/created", "inbox/accepted", "inbox/claimed"]
    # The note explains it, and never masquerades as a timeline row.
    assert console.has("Timeline shows selected persisted events.")
    assert console.has("are Event Log seq values")
    assert not any(line.startswith("[event N]") for line in console.lines)


async def test_the_seq_note_is_printed_once(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="Done."),), repeat_last=True)
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("one", "two", "/exit"))

    await run_chat(runtime, console.console, workspace=tmp_path, heartbeat_seconds=0.0)

    assert console.lines.count("Timeline shows selected persisted events.") == 1


async def test_continuing_a_session_explains_a_high_first_seq(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="One."),), repeat_last=True)
    first = build_runtime(tmp_path, provider)
    store = first.sessions.store
    session_id = await first.create_session(tmp_path)
    await first.run_existing(session_id, "first turn")
    await first.dispose()
    history = len(await first.sessions.read_session(session_id))

    second = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / ".traceh", provider="scripted", model="qwen-plus"),
        provider=ScriptedLlmProvider((ModelResponse(content="Two."),), repeat_last=True),
        event_store=store,
    )
    console = FakeConsole(("second turn", "/exit"))
    await run_chat(second, console.console, session_id=session_id, heartbeat_seconds=0.0)

    shown = console.shown_seqs()
    assert shown, "the new turn is still narrated"
    assert min(shown) > history, "history must not be repainted"
    assert console.lines.count("Timeline shows selected persisted events.") == 1
    assert console.index_of("Timeline shows selected") < console.index_of("[event ")
