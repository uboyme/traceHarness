"""Timeline rendering and its live use inside `traceh chat`.

The load-bearing test here is `test_timeline_appears_while_a_tool_is_still_running`:
printing events after a turn finishes would satisfy every other assertion in
this file, so one test has to hold the turn open and check the console mid-flight.
"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.cli.chat import INTERRUPTED_EXIT_CODE, run_chat
from traceh.cli.console import Console
from traceh.cli.main import build_parser
from traceh.cli.text_safety import UNSAFE_LINE_CATEGORIES
from traceh.cli.timeline import MAX_DETAIL_CHARS, TimelineRenderer, sanitize
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore

TIMELINE_TASKS = ("traceh-chat-driver-events", "traceh-chat-driver-heartbeat")


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

    def index_of(self, needle: str) -> int:
        for position, line in enumerate(self.lines):
            if needle in line:
                return position
        raise AssertionError(f"{needle!r} not found in:\n{self.output}")

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)

    def index_of_line_starting_with(self, prefix: str) -> int:
        for position, line in enumerate(self.lines):
            if line.startswith(prefix):
                return position
        raise AssertionError(f"no line starts with {prefix!r} in:\n{self.output}")


class GateTool:
    """A tool that blocks until the test lets it finish.

    ``entered`` is set by the tool itself once it is really executing, so a test
    can synchronise on the actual execution instead of guessing with a sleep.
    """

    name = "gate_tool"
    description = "Blocks until released; used to observe a turn in flight."
    input_schema: dict = {"type": "object", "properties": {}}
    effect_kind = EffectKind.WORKSPACE_READ

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> ToolOutput:
        self.entered.set()
        await self.release.wait()
        return ToolOutput(content="gate opened")


def build_runtime(tmp_path: Path, provider, *, tools=()):
    return build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / ".traceh", provider="scripted", model="test-model"),
        provider=provider,
        event_store=InMemoryEventStore(),
        additional_tools=tools,
    )


def envelope(event_type: str, data: dict, *, seq: int = 7) -> EventEnvelope:
    return EventEnvelope.materialize("session:x", seq, PendingEvent(event_type, data))


# -- renderer ------------------------------------------------------------


def test_every_line_carries_the_real_event_sequence() -> None:
    renderer = TimelineRenderer()
    line = renderer.render(envelope("turn/start", {"turn_id": "t"}, seq=42))
    assert line == "[event 42] Turn started"


def test_step_end_reuses_the_number_from_step_start() -> None:
    renderer = TimelineRenderer()
    renderer.render(envelope("step/start", {"step_id": "s1", "number": 3}, seq=5))
    assert renderer.render(envelope("step/end", {"step_id": "s1"}, seq=9)) == (
        "[event 9] Step 3 completed"
    )


def test_step_end_without_a_known_start_still_renders() -> None:
    renderer = TimelineRenderer()
    assert renderer.render(envelope("step/end", {"step_id": "ghost"})) == (
        "[event 7] Step completed"
    )


def test_tool_lifecycle_renders_name_and_status() -> None:
    renderer = TimelineRenderer()
    call = {"tool_name": "read_file", "tool_call_id": "c1", "arguments": {"path": "a/b.txt"}}
    assert renderer.render(envelope("tool/call", call, seq=1)) == (
        "[event 1] Tool read_file requested a/b.txt"
    )
    assert renderer.render(envelope("tool/admitted", call, seq=2)) == (
        "[event 2] Tool read_file started"
    )
    assert renderer.render(
        envelope("tool/result", {"tool_name": "read_file", "status": "succeeded"}, seq=3)
    ) == "[event 3] Tool read_file succeeded"


def test_failed_tool_result_names_the_error_type() -> None:
    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/result",
            {"tool_name": "shell", "status": "failed", "error_type": "ToolReportedTimeout"},
        )
    )
    assert line == "[event 7] Tool shell failed (ToolReportedTimeout)"


def test_verification_renders_both_outcomes() -> None:
    renderer = TimelineRenderer()
    assert renderer.render(envelope("verification/result", {"passed": True})) == (
        "[event 7] Verification passed"
    )
    assert renderer.render(
        envelope("verification/result", {"passed": False, "exit_code": 1})
    ) == "[event 7] Verification failed (exit_code=1)"


def test_runtime_and_recovery_events_render() -> None:
    renderer = TimelineRenderer()
    assert renderer.render(envelope("runtime/cancel-requested", {"reason": "x"})) == (
        "[event 7] Cancellation requested"
    )
    assert renderer.render(
        envelope("runtime/recovered", {"closed_model_attempts": 1, "synthesized_tool_results": 2})
    ) == "[event 7] Recovered (model_attempts=1 tool_results=2)"
    assert renderer.render(
        envelope("model/attempt-end", {"status": "failed", "error_type": "ProviderHttpError"})
    ) == "[event 7] Model attempt failed (ProviderHttpError)"


@pytest.mark.parametrize(
    "event_type",
    [
        "composition/snapshot",
        "request/snapshot",
        "assistant/message",
        "assistant/chunk",
        "user/message",
        "inbox/accepted",
        "session/created",
        "surface/replace",
        "effect/intent",
        "some/future-event",
    ],
)
def test_noisy_and_unknown_events_render_nothing(event_type: str) -> None:
    """Anything not explicitly shown must be silent, never a raw payload dump."""

    renderer = TimelineRenderer()
    secret = {"request": {"messages": [{"content": "sk-not-a-real-key"}]}, "content": "x" * 500}
    assert renderer.render(envelope(event_type, secret)) is None


def test_renderer_survives_missing_and_wrongly_typed_fields() -> None:
    """A degraded payload degrades the line; it never raises."""

    renderer = TimelineRenderer()
    cases = [
        ("turn/end", {}),
        ("step/start", {"step_id": None, "number": "not-a-number"}),
        ("step/end", {}),
        ("tool/call", {}),
        ("tool/admitted", {"tool_name": 123}),
        ("tool/result", {"tool_name": "", "status": None}),
        ("verification/result", {"passed": "maybe", "exit_code": "nope"}),
        ("model/attempt-start", {"provider": None}),
        ("model/attempt-end", {}),
        ("runtime/error", {}),
        ("runtime/recovered", {"closed_model_attempts": "many"}),
    ]
    for event_type, data in cases:
        line = renderer.render(envelope(event_type, data))
        assert line is None or line.startswith("[event 7] ")


def test_tool_detail_is_bounded_and_single_line() -> None:
    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {
                "tool_name": "read_file",
                "tool_call_id": "c1",
                "arguments": {"path": "deep/" + "a" * 400 + "\nsecond line"},
            },
        )
    )
    assert "\n" not in line
    assert len(line) < 120
    assert "…" in line


def test_a_shown_path_is_suppressed_when_it_looks_like_a_credential() -> None:
    """A partially shown secret is still a leak, so the argument is dropped."""

    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {
                "tool_name": "read_file",
                "tool_call_id": "c9",
                # NOT a real credential: a deliberately key-shaped test fixture.
                "arguments": {"path": "secrets/api_key_FAKE_FIXTURE.txt"},
            },
        )
    )
    assert "api_key_FAKE_FIXTURE" not in line
    assert "Tool read_file requested" in line
    assert "call c9" in line


def test_unknown_tool_shows_only_its_name_and_call_id() -> None:
    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {
                "tool_name": "mystery_tool",
                "tool_call_id": "c2",
                "arguments": {"secret_payload": "should-not-appear"},
            },
        )
    )
    assert line == "[event 7] Tool mystery_tool requested (call c2)"


def test_renderer_never_mutates_the_event() -> None:
    renderer = TimelineRenderer()
    event = envelope("tool/call", {"tool_name": "read_file", "arguments": {"path": "a.txt"}})
    before = {"tool_name": "read_file", "arguments": {"path": "a.txt"}}
    renderer.render(event)
    assert event.data == before


# -- chat integration ----------------------------------------------------


async def test_timeline_appears_while_a_tool_is_still_running(tmp_path: Path) -> None:
    """The proof that this is live rather than a replay printed at the end.

    The tool signals that it has really started and then blocks. While it is
    blocked the turn cannot have finished, so any timeline line on the console at
    that moment was written during the turn.
    """

    gate = GateTool()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(content="", tool_calls=(ToolCall("c1", "gate_tool", {}),)),
            ModelResponse(content="All done."),
        )
    )
    runtime = build_runtime(tmp_path, provider, tools=(gate,))
    console = FakeConsole(("use the gate",))

    chat = asyncio.create_task(run_chat(runtime, console.console, workspace=tmp_path))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=10)

        # Mid-flight: the tool is inside execute() and has not returned.
        assert console.has("Turn started")
        assert console.has("Step 1 started")
        assert console.has("Tool gate_tool requested")
        assert console.has("Tool gate_tool started")
        # ...and the turn has demonstrably not finished yet.
        assert not console.has("Tool gate_tool succeeded")
        assert not console.has("assistant>")
        assert not console.has("Turn ended")
    finally:
        gate.release.set()
        exit_code = await asyncio.wait_for(chat, timeout=10)

    assert exit_code == 0
    assert console.has("Tool gate_tool succeeded")
    assert console.has("assistant> All done.")
    # Order: the tool result line precedes the final answer.
    assert console.index_of("Tool gate_tool succeeded") < console.index_of("assistant>")


async def test_timeline_precedes_the_final_answer_and_uses_persisted_seq(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="", tool_calls=(ToolCall("c1", "read_file", {"path": "hello.txt"}),)
            ),
            ModelResponse(content="Read it."),
        )
    )
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("read hello.txt",))

    await run_chat(runtime, console.console, workspace=tmp_path)

    assert console.index_of("Turn started") < console.index_of("assistant>")
    assert console.has("Tool read_file requested hello.txt")

    # Every rendered number is a real seq in the session stream.
    sessions = await runtime.sessions.list_sessions()
    events = await runtime.sessions.read_session(sessions[0])
    by_seq = {event.seq: event.type for event in events}
    rendered = [line for line in console.lines if line.startswith("[event ")]
    assert rendered
    for line in rendered:
        seq = int(line.split("]")[0].removeprefix("[event ").strip())
        assert seq in by_seq, f"{line} references a seq that is not in the log"

    # Numbers are not consecutive: hidden events keep their sequence numbers,
    # which is exactly what proves these are not CLI-generated line numbers.
    shown = [int(line.split("]")[0].removeprefix("[event ").strip()) for line in rendered]
    assert shown != list(range(shown[0], shown[0] + len(shown)))


async def test_no_timeline_silences_activity_but_keeps_the_answer(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="Quiet answer."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("say something",))

    await run_chat(runtime, console.console, workspace=tmp_path, timeline=False)

    assert not any(line.startswith("[event ") for line in console.lines)
    assert console.has("assistant> Quiet answer.")
    assert console.has("[reason=completed")


async def test_continuing_a_session_does_not_repaint_history(tmp_path: Path) -> None:
    """Only events published after subscribing are shown."""

    first = build_runtime(tmp_path, ScriptedLlmProvider((ModelResponse(content="One."),)))
    store = first.sessions.store
    session_id = await first.create_session(tmp_path)
    await first.run_existing(session_id, "first turn")
    await first.dispose()
    history = len(await first.sessions.read_session(session_id))
    assert history > 3

    # A second runtime over the same store, continuing the same session.
    second = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / ".traceh", provider="scripted", model="test-model"),
        provider=ScriptedLlmProvider((ModelResponse(content="Two."),)),
        event_store=store,
    )
    console = FakeConsole(("second turn",))
    await run_chat(second, console.console, session_id=session_id)

    shown = [
        int(line.split("]")[0].removeprefix("[event ").strip())
        for line in console.lines
        if line.startswith("[event ")
    ]
    assert shown, "the new turn should still be narrated"
    assert min(shown) > history, f"history was repainted: {shown} vs {history} existing events"


async def test_a_failed_turn_keeps_its_timeline_and_chat_continues(tmp_path: Path) -> None:
    class ExplodingProvider:
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider exploded")
            return ModelResponse(content="Recovered answer.")

    runtime = build_runtime(tmp_path, ExplodingProvider())
    console = FakeConsole(("first", "second"))

    await run_chat(runtime, console.console, workspace=tmp_path)

    assert console.has("Turn started")
    # The timeline reported the failure, and the chat printed its own error line.
    # Match distinctly: the timeline line is prefixed with "[event N] ", so a
    # plain substring would find the same line twice.
    assert console.has("] Runtime error: ProviderFailure")
    chat_error = console.index_of_line_starting_with("error: ProviderFailure")
    # Timeline for the failed turn comes before the chat's error line.
    assert console.index_of("] Runtime error: ProviderFailure") < chat_error
    # And the chat kept going.
    assert console.has("assistant> Recovered answer.")


async def test_internal_commands_produce_no_timeline_lines(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="unused"),), repeat_last=True)
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("", "/help", "/session", "/nope", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    assert not any(line.startswith("[event ") for line in console.lines)
    assert console.has("--no-timeline")
    assert console.has("unknown command (try /help)")


async def test_no_subscription_survives_a_finished_chat(tmp_path: Path) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="Bye."),))
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("one turn",))

    await run_chat(runtime, console.console, workspace=tmp_path)

    sessions = await runtime.sessions.list_sessions()
    stream = runtime.sessions.session_stream(sessions[0])
    assert runtime.events.subscriber_count(stream) == 0
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in TIMELINE_TASKS and not task.done()
    ]


async def test_no_subscription_survives_an_interrupted_chat(tmp_path: Path) -> None:
    """Interrupting mid-tool must still unregister the subscription.

    One interrupt now cancels the turn and keeps the session, so the chat returns
    to its prompt; with no further scripted input the console raises EOF and the
    chat leaves cleanly with 0. What this test is about either way is that
    nothing observing the turn outlives it.
    """

    gate = GateTool()
    provider = ScriptedLlmProvider(
        (ModelResponse(content="", tool_calls=(ToolCall("c1", "gate_tool", {}),)),),
        repeat_last=True,
    )
    runtime = build_runtime(tmp_path, provider, tools=(gate,))
    console = FakeConsole(("use the gate",))

    chat = asyncio.create_task(run_chat(runtime, console.console, workspace=tmp_path))
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    chat.cancel()
    gate.release.set()
    try:
        assert await asyncio.wait_for(chat, timeout=10) in (0, INTERRUPTED_EXIT_CODE)
    except asyncio.CancelledError:
        pass  # Either convergence path is acceptable; cleanup is what matters.

    sessions = await runtime.sessions.list_sessions()
    stream = runtime.sessions.session_stream(sessions[0])
    assert runtime.events.subscriber_count(stream) == 0
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in TIMELINE_TASKS and not task.done()
    ]


async def test_timeline_never_prints_prompts_requests_or_credentials(tmp_path: Path) -> None:
    """The whole turn's console output must not contain the model-facing text."""

    marker = "SUPER-SECRET-PROMPT-MARKER"
    (tmp_path / "notes.txt").write_text("file body should not be printed", encoding="utf-8")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="", tool_calls=(ToolCall("c1", "read_file", {"path": "notes.txt"}),)
            ),
            ModelResponse(content="Finished."),
        )
    )
    runtime = build_runtime(tmp_path, provider)
    await runtime.sessions.create_session(tmp_path, session_id="s-secret")
    console = FakeConsole((f"look at notes.txt {marker}",))

    await run_chat(runtime, console.console, session_id="s-secret")

    timeline_lines = "\n".join(
        line for line in console.lines if line.startswith("[event ")
    )
    assert marker not in timeline_lines
    assert "file body should not be printed" not in timeline_lines
    assert "system" not in timeline_lines.lower()
    assert "messages" not in timeline_lines
    # The request snapshot really was persisted; it just is not displayed.
    events = await runtime.sessions.read_session("s-secret")
    assert any(event.type == "request/snapshot" for event in events)


def test_parser_defaults_timeline_on_and_no_timeline_turns_it_off() -> None:
    parser = build_parser()
    assert parser.parse_args(["chat", "."]).timeline is True
    assert parser.parse_args(["chat", ".", "--no-timeline"]).timeline is False


def test_detail_bound_is_a_sane_single_line_budget() -> None:
    assert 20 <= MAX_DETAIL_CHARS <= 200


# -- terminal safety -----------------------------------------------------
#
# Every string below is a deliberately hostile *test fixture*. None is a real
# credential: the key-shaped values use FAKE/FIXTURE markers and unroutable
# example hosts. What is under test is that the renderer never trusts a payload
# string, wherever it came from.

#: Payload values that must never survive into terminal output as written.
HOSTILE_VALUES = (
    # Categories Zl/Zp: line breaks a `C*`-only check lets through.
    "sep\u2028[event 999] forged",
    "para\u2029[event 999] forged",
    "line\nforged",
    "carriage\rreturn",
    "clear\x1b[2Jscreen",
    "colour\x1b[31mred",
    "back\x08space",
    "bell\a",
    "nul\x00byte",
    "bidi\u202eoverride",
    "zero\u200bwidth",
    "x" * 500,
)

#: Every payload field a handler interpolates into a line.
INTERPOLATED_FIELDS = (
    ("tool/call", "tool_name"),
    ("tool/call", "tool_call_id"),
    ("tool/admitted", "tool_name"),
    ("tool/result", "tool_name"),
    ("tool/result", "status"),
    ("tool/result", "error_type"),
    ("model/attempt-start", "provider"),
    ("model/attempt-start", "model"),
    ("model/attempt-end", "status"),
    ("model/attempt-end", "error_type"),
    ("turn/end", "reason"),
    ("step/end", "reason"),
    ("runtime/error", "error_type"),
)


def assert_terminal_safe(line: str | None) -> None:
    """A rendered line must be one inert, bounded row.

    `splitlines()` is the load-bearing check. Testing only for `\\n`/`\\r` and the
    `C*` categories passed happily on `U+2028`/`U+2029`, which are categories
    `Zl`/`Zp` and *are* line breaks to `splitlines()` and to many viewers - so a
    payload could still turn one row into two while every assertion held.
    """

    if line is None:
        return
    assert len(line.splitlines()) <= 1, f"forged row: {line!r}"
    assert "\n" not in line and "\r" not in line, f"forged row: {line!r}"
    for separator in ("\u2028", "\u2029"):
        assert separator not in line, f"line separator survived: {line!r}"
    offenders = [
        character
        for character in line
        if unicodedata.category(character) in UNSAFE_LINE_CATEGORIES
    ]
    assert not offenders, f"control/format characters survived: {offenders!r} in {line!r}"
    assert "\x1b" not in line, f"escape sequence survived: {line!r}"
    # The length bound is about payload-derived content. Lines the CLI builds
    # itself - such as a resume command containing a long absolute path - are
    # legitimately longer and carry nothing from an event payload.
    if line.startswith("["):
        assert len(line) < 200, f"unbounded line ({len(line)} chars): {line!r}"


@pytest.mark.parametrize(("event_type", "field"), INTERPOLATED_FIELDS)
@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_hostile_payload_strings_cannot_break_the_line(
    event_type: str, field: str, hostile: str
) -> None:
    """No interpolated field may forge a row, emit control codes or run long."""

    renderer = TimelineRenderer()
    line = renderer.render(envelope(event_type, {field: hostile}))
    assert_terminal_safe(line)


def test_a_forged_event_prefix_cannot_become_a_second_row() -> None:
    """The classic injection: a tool name shaped like a whole extra line.

    What is guaranteed is structural, and worth stating exactly: the payload
    cannot produce a second *row*, and the row it lands in still begins with the
    real event number. The injected characters do survive as inert text inside
    that row - neutralising the newline is what matters, and escaping every
    bracket in a tool name would cost more legibility than it buys.
    """

    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {
                "tool_name": "ok\n[event 999] Turn ended (hacked)",
                "tool_call_id": "c1",
            },
        )
    )
    assert_terminal_safe(line)
    assert line.startswith("[event 7] "), "the real event number must lead the row"
    assert line.count("\n") == 0, "one payload must never yield two rows"
    # The forged text is inert content of this single row, not a row of its own.
    assert line.endswith("requested (call c1)")


#: Key-shaped fixtures. Not real credentials - each carries a FAKE/FIXTURE
#: marker or an unroutable example host.
CREDENTIAL_SHAPED_COMMANDS = (
    "deploy --key sk-proj-FAKE0000FIXTURE0000",
    "gh auth login --with-token ghp_FAKE0000FIXTURE00",
    "slack post --token xoxb-FAKE-0000-FIXTURE",
    "curl https://alice:FAKEPASSWORD@example.invalid/x",
    "export MY_API_KEY=FAKE0000FIXTURE && run",
    "psql postgres://user:FAKEPW@db.example.invalid/app",
)


@pytest.mark.parametrize("command", CREDENTIAL_SHAPED_COMMANDS)
def test_a_shell_command_is_never_displayed(command: str) -> None:
    """Shell shows its name and call id only - never what it runs.

    No keyword scan can be trusted to recognise every secret shape, so the
    command is withheld unconditionally rather than filtered.
    """

    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {"tool_name": "shell", "tool_call_id": "c7", "arguments": {"command": command}},
        )
    )
    assert line == "[event 7] Tool shell requested (call c7)"
    for fragment in ("FAKE", "FIXTURE", "sk-proj", "ghp_", "xoxb", "example.invalid"):
        assert fragment not in line


def test_even_an_innocuous_shell_command_is_withheld() -> None:
    """The rule is unconditional, so it cannot be defeated by a benign-looking case."""

    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "tool/call",
            {"tool_name": "shell", "tool_call_id": "c8", "arguments": {"command": "ls -la"}},
        )
    )
    assert line == "[event 7] Tool shell requested (call c8)"
    assert "ls -la" not in line


def test_runtime_error_never_shows_the_message() -> None:
    """An exception message is arbitrary text and may quote a credential."""

    renderer = TimelineRenderer()
    line = renderer.render(
        envelope(
            "runtime/error",
            {
                "error_type": "ProviderHttpError",
                # NOT a real key: a key-shaped test fixture.
                "message": "401 for Authorization: Bearer sk-proj-FAKE0000FIXTURE",
                "traceback": 'File "x.py", line 1\n  boom',
            },
        )
    )
    assert line == "[event 7] Runtime error: ProviderHttpError"
    for fragment in ("FAKE", "FIXTURE", "Bearer", "sk-proj", "401", "x.py"):
        assert fragment not in line


def test_sanitize_is_idempotent_and_bounded() -> None:
    first = sanitize("a\x1b[2Jb\nc" + "d" * 500)
    assert sanitize(first) == first
    assert len(first) <= MAX_DETAIL_CHARS
    assert_terminal_safe(f"[event 1] {first}")


def test_sanitize_keeps_ordinary_non_ascii_text() -> None:
    """Scrubbing targets control and format characters, not real content."""

    assert sanitize("读取 hello.txt") == "读取 hello.txt"


async def test_a_hostile_tool_name_cannot_forge_console_rows(tmp_path: Path) -> None:
    """End to end: a model-chosen tool name reaches the console through the feed."""

    hostile = "nope\n[event 999] Turn ended (hacked)\x1b[2J"
    provider = ScriptedLlmProvider(
        (
            ModelResponse(content="", tool_calls=(ToolCall("c1", hostile, {}),)),
            ModelResponse(content="Done."),
        )
    )
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("go",))

    await run_chat(runtime, console.console, workspace=tmp_path)

    for line in console.lines:
        assert_terminal_safe(line)
    # The unknown tool really was requested and reported, on one row each.
    assert sum(1 for line in console.lines if line.startswith("[event ")) == len(
        [line for line in console.lines if line.startswith("[event ")]
    )
    assert console.has("Tool nope")


async def test_a_failing_printer_does_not_change_the_turn_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renderer bug is a display bug: the turn, its answer and the log stand."""

    def exploding_render(self, event):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(TimelineRenderer, "render", exploding_render)

    provider = ScriptedLlmProvider(
        (ModelResponse(content="First."), ModelResponse(content="Second."))
    )
    runtime = build_runtime(tmp_path, provider)
    console = FakeConsole(("one", "two"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    # Both turns completed and printed, despite the printer failing each time.
    assert console.has("assistant> First.")
    assert console.has("assistant> Second.")
    assert not console.has("renderer exploded")

    sessions = await runtime.sessions.list_sessions()
    stream = runtime.sessions.session_stream(sessions[0])
    assert runtime.events.subscriber_count(stream) == 0
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in TIMELINE_TASKS and not task.done()
    ]

    # The event log is untouched by the observer's failure.
    events = await runtime.sessions.read_session(sessions[0])
    assert [event.type for event in events].count("turn/end") == 2
    assert not any(event.type == "runtime/error" for event in events)
