"""Interactive multi-turn chat loop.

The loop owns no conversation state. Every turn goes through
``AgentRuntime.run_existing()``, so the Session event log stays the single
source of truth and the model history is projected from it, exactly as for the
one-shot commands. What lives here is only terminal behaviour: prompting,
internal commands, rendering a turn result and converging on exit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from traceh.cli.activity import (
    DEFAULT_HEARTBEAT_SECONDS,
    ActivityTracker,
    Clock,
    default_clock,
)
from traceh.cli.command_line import (
    Literal,
    UnsafeCommandValue,
    default_shell,
    escape_for_display,
    is_renderable,
    render_command,
)
from traceh.cli.console import Console, contains_undecodable_input, normalize_input
from traceh.cli.env_file import is_env_var_name
from traceh.cli.errors import CliConfigurationError
from traceh.cli.timeline import TimelineRenderer
from traceh.concurrency import await_worker_convergence
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.event_feed import EventSubscription
from traceh.session.service import SessionNotFoundError

if TYPE_CHECKING:
    from traceh.product.chat import ProductChatSurface, ProductChatTurn

PROMPT = "you> "
ASSISTANT_PREFIX = "assistant> "

#: Conventional shell exit code for "terminated by SIGINT".
INTERRUPTED_EXIT_CODE = 130

#: Shown after a turn is interrupted, to make clear the session survived.
INTERRUPTED_TURN_NOTICE = "Turn interrupted. This session is still open."

#: Printed once at startup when the timeline is on. Without it the first line
#: reads `[event 4]` and looks like something was lost; the real reason is that
#: `session/created`, `inbox/accepted` and `inbox/claimed` are persisted but not
#: displayed. Renumbering to 1 would destroy the property that makes the number
#: useful - being the seq you can look up in the JSONL.
#: Note the wording never *begins* with the bracket: a line starting with
#: `[event ...]` is a timeline row, and an explanation that imitates one would be
#: the first thing to confuse both a reader and a log filter.
_SEQ_NOTE_LINES = (
    "Timeline shows selected persisted events.",
    "Numbers shown as [event N] are Event Log seq values; they may start above 1 "
    "or skip where internal events are hidden.",
)

_HELP_LINES = (
    "/help     show these commands",
    "/session  show the session id, workspace, provider, model and resume command",
    "/plugins                 show active plugins",
    "/plugins reload          rebuild the active plugin composition",
    "/plugins use ID [ID ...] switch this session to installed plugins",
    "/plugins use --none      switch this session to no external plugins",
    "/task inspect TASK_ID    inspect a durable ProductTask (with --product-config)",
    "/task approve TASK_ID    approve and promote a verified ProductTask",
    "/task reject TASK_ID     reject without moving the target ref",
    "/task cancel TASK_ID     cancel owned work and release resources",
    "/task abandon TASK_ID    record an unowned interrupted task as abandoned",
    "/exit     leave the chat",
    "/quit     leave the chat",
    "",
    "Install plugins outside chat, then inspect and enable them here:",
    "  python -m pip install <plugin-wheel-or-package>",
    "  traceh plugins list",
    "  traceh plugins doctor ID",
    "",
    "Startup flags, not commands you can type here:",
    "  --no-timeline          silence live activity output",
    "  --heartbeat-seconds N  seconds between waiting notices (0 disables)",
)


@dataclass(frozen=True, slots=True)
class ChatSession:
    session_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class ResumeEnvironment:
    """Settings the resume command needs but `RuntimeConfig` does not carry.

    A resume command holding only a session id and a data dir looks sufficient
    and is not: provider and model may have come from a `.env` that the next
    shell will not find, so re-running it can silently continue the session on a
    different model.

    ``api_key_env`` is the *name* of the variable holding the key; the value is
    never read here and never printed. ``env_file_supplies`` lists the variables
    the env file actually applied, which is how the block can tell "this will be
    reloaded for you" from "you must supply this again" for the key itself.

    ``verifier_from_env_file`` is a **provenance flag, not the command**. Whether
    the env file can restore the verifier depends on which value actually won:
    an explicit ``--verify-command`` overrides the file, so the file containing
    that key proves nothing. Only argument resolution knows the answer, so it is
    decided there and passed here as a boolean. The verifier text itself is
    deliberately absent from this dataclass, keeping it out of the repr, the
    command and any log line.
    """

    base_url: str | None = None
    api_key_env: str | None = None
    env_file: Path | None = None
    script: Path | None = None
    env_file_supplies: frozenset[str] = frozenset()
    verifier_from_env_file: bool = False
    product_config: Path | None = None


def _safe_base_url(value: str | None) -> tuple[str | None, str | None]:
    """Return the URL to display, or ``None`` plus the reason it was withheld.

    A base URL can carry credentials in two structural places, and both are
    checked by parsing rather than by pattern-matching the string: ``userinfo``
    (``https://user:password@host``) and the query string. Withholding on *any*
    query is deliberate - it needs no judgement about which parameter names are
    sensitive, and a base URL with a query is rare enough that refusing is
    cheaper than being wrong.

    This is a structural rule, not a secret detector: it cannot know whether an
    ordinary-looking path segment is itself a credential.
    """

    if not value:
        return None, None
    if not is_renderable(value):
        return None, "it contains control characters"
    try:
        parsed = urlparse(value)
        # Both the parse and the userinfo accessors can raise: `https://[bad`
        # fails with "Invalid IPv6 URL" only once the netloc is inspected.
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        # An unparseable URL is withheld rather than echoed. It cannot be shown
        # to be credential-free, and reporting it would put the original value -
        # and a traceback - in front of the user.
        return None, "it could not be parsed as a URL"
    if has_userinfo:
        return None, "it embeds credentials in the URL"
    if parsed.query or parsed.fragment:
        return None, "it carries a query string that may contain credentials"
    return value, None


@dataclass(frozen=True, slots=True)
class _TurnDisplay:
    """The observation machinery for one turn, so it can be torn down as a unit.

    Kept together because the failure mode of tracking these separately is a
    task that outlives the turn and keeps writing to the console.
    """

    subscription: EventSubscription | None = None
    printer: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None


def chat_target(workspace: Path | None, session_id: str | None) -> tuple[Path | None, str | None]:
    """Validate that exactly one of workspace / session id was requested."""

    if workspace is not None and session_id is not None:
        raise CliConfigurationError(
            "chat takes either a workspace or --session-id, not both"
        )
    if workspace is None and session_id is None:
        raise CliConfigurationError(
            "chat needs a workspace to start a new session, or --session-id to continue one"
        )
    return workspace, session_id


async def run_chat(
    runtime: AgentRuntime,
    console: Console,
    *,
    workspace: Path | None = None,
    session_id: str | None = None,
    timeline: bool = True,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    clock: Clock | None = None,
    resume_environment: ResumeEnvironment | None = None,
    product: ProductChatSurface | None = None,
) -> int:
    """Open or continue a session, then read turns until the user leaves."""

    workspace, session_id = chat_target(workspace, session_id)
    resolved_clock = clock or default_clock()
    environment = resume_environment or ResumeEnvironment()
    interval = heartbeat_seconds if timeline else 0.0
    try:
        session = await _open_session(
            runtime,
            console,
            workspace=workspace,
            session_id=session_id,
            timeline=timeline,
            resume_environment=environment,
        )
        try:
            return await _chat_loop(
                runtime,
                console,
                session,
                timeline=timeline,
                heartbeat_seconds=interval,
                clock=resolved_clock,
                resume_environment=environment,
                product=product,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Reached only for an interrupt the loop did not already absorb - an
            # idle Ctrl+C returns from the loop itself, and an interrupted turn
            # converges inside `_run_turn`. This is the top-level coroutine of
            # the process, so turning what is left into an exit code is the job.
            await _converge_interrupted(runtime, console, session, environment)
            return INTERRUPTED_EXIT_CODE
    finally:
        failures: list[BaseException] = []
        if product is not None:
            try:
                await product.aclose()
            except BaseException as error:
                failures.append(error)
        try:
            await runtime.dispose()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise BaseExceptionGroup("chat shutdown failed", failures)


async def _open_session(
    runtime: AgentRuntime,
    console: Console,
    *,
    workspace: Path | None,
    session_id: str | None,
    timeline: bool,
    resume_environment: ResumeEnvironment,
) -> ChatSession:
    if session_id is None:
        if workspace is None:  # pragma: no cover - chat_target already rejects this
            raise CliConfigurationError("chat needs a workspace to start a new session")
        try:
            created = await runtime.create_session(workspace, metadata={"cli": "chat"})
        except NotADirectoryError as error:
            raise CliConfigurationError(f"workspace is not a directory: {error}") from error
        # Read it back so the banner shows the workspace exactly as persisted.
        session = ChatSession(created, await runtime.sessions.workspace_for(created))
        await _write_session_banner(runtime, console, session, resume_environment)
        _write_seq_note(console, timeline=timeline)
        return session

    try:
        resolved_workspace = await runtime.sessions.workspace_for(session_id)
    except SessionNotFoundError as error:
        raise CliConfigurationError(f"session not found: {session_id}") from error
    session = ChatSession(session_id, resolved_workspace)

    # Converge whatever the previous process left open before the user can add
    # anything new. No turn is started and no instruction is injected: the next
    # turn is the one the user types.
    #
    # Checked before recovery, because recovery appends events: a session created
    # under a different plugin set must be refused, not repaired and continued.
    await runtime.verify_session_plugins(session_id)
    report = await runtime.recovery.recover(session_id)
    await _write_session_banner(runtime, console, session, resume_environment)
    # Continuing a session needs this note more, not less: the first new event
    # can be seq 40 or 400 with nothing before it on screen.
    _write_seq_note(console, timeline=timeline)
    if report.changed:
        console.write(
            "recovered: "
            f"model_attempts={report.closed_model_attempts} "
            f"tool_results={report.synthesized_tool_results} "
            f"step={report.closed_step} turn={report.closed_turn}"
        )
    return session


async def _chat_loop(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    *,
    timeline: bool,
    heartbeat_seconds: float,
    clock: Clock,
    resume_environment: ResumeEnvironment,
    product: ProductChatSurface | None = None,
) -> int:
    console.write("Type /help for commands, /exit to leave.")
    while True:
        try:
            line = console.read_line(PROMPT)
        except EOFError:
            console.write("")
            await _write_resume_block_for_session(runtime, console, session, resume_environment)
            return 0
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Idle interrupt: there is no turn to converge, so leaving is the
            # honest response. The resume block is reprinted because this is the
            # moment the user most needs it.
            console.write("")
            await _write_resume_block_for_session(runtime, console, session, resume_environment)
            return INTERRUPTED_EXIT_CODE

        text = normalize_input(line)
        if not text:
            continue
        if contains_undecodable_input(text):
            console.write(
                "input contains characters that could not be decoded and was not sent. "
                "Send the text as UTF-8; TraceHarness will not guess what it was."
            )
            continue
        if text.startswith("/"):
            if await _handle_command(
                runtime,
                console,
                session,
                text,
                resume_environment,
                product=product,
            ):
                await _write_resume_block_for_session(
                    runtime, console, session, resume_environment
                )
                return 0
            continue
        interrupted_twice = await _run_turn(
            runtime,
            console,
            session,
            text,
            timeline=timeline,
            heartbeat_seconds=heartbeat_seconds,
            clock=clock,
            product=product,
        )
        if interrupted_twice:
            # The user asked again while the first interrupt was still
            # converging. Convergence finished first; now honour the second ask.
            await _write_resume_block_for_session(runtime, console, session, resume_environment)
            return INTERRUPTED_EXIT_CODE


def _active_plugins_line(runtime: AgentRuntime) -> str:
    identities = runtime.external_plugin_identities
    if not identities:
        return "active plugins: none"
    rendered = ", ".join(
        f"{identity.plugin_id}=={identity.version}" for identity in identities
    )
    return f"active plugins: {rendered}"


async def _handle_command(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    text: str,
    resume_environment: ResumeEnvironment,
    *,
    product: ProductChatSurface | None = None,
) -> bool:
    """Handle one internal command. Returns True when the chat should end."""

    if text in {"/exit", "/quit"}:
        return True
    if product is not None and await product.handle_command(text, console):
        return False
    if text == "/help":
        for entry in _HELP_LINES:
            console.write(entry)
        return False
    if text == "/session":
        await _write_session_banner(runtime, console, session, resume_environment)
        return False
    if text == "/plugins":
        console.write(_active_plugins_line(runtime))
        return False
    if text == "/plugins reload":
        try:
            await runtime.reload_plugin_composition(session.session_id)
        except Exception:
            console.write("plugin composition change failed")
        else:
            console.write("plugin composition reloaded")
            console.write(_active_plugins_line(runtime))
        return False
    if text == "/plugins use" or text.startswith("/plugins use "):
        parts = text.split()
        if parts == ["/plugins", "use", "--none"]:
            enabled_plugin_ids: tuple[str, ...] = ()
        elif len(parts) >= 3 and "--none" not in parts[2:]:
            enabled_plugin_ids = tuple(parts[2:])
        else:
            console.write("usage: /plugins use ID [ID ...] or /plugins use --none")
            return False
        try:
            await runtime.migrate_session_plugin_composition(
                session.session_id,
                enabled_plugin_ids,
            )
        except Exception:
            # Selection and third-party activation failures are deliberately
            # not rendered here.  Their details may contain paths, plugin text
            # or configuration secrets; the runtime already retains only
            # bounded structured diagnostics for programmatic callers.
            console.write("plugin composition change failed")
        else:
            console.write("plugin composition switched")
            console.write(_active_plugins_line(runtime))
        return False
    if text.startswith("/plugins "):
        console.write("unknown plugin command (try /help)")
        return False
    console.write("unknown command (try /help)")
    return False


async def _run_turn(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    text: str,
    *,
    timeline: bool,
    heartbeat_seconds: float,
    clock: Clock,
    product: ProductChatSurface | None = None,
) -> bool:
    """Run one turn, narrating it while it happens.

    Returns ``True`` when the user interrupted more than once, which the caller
    reads as "leave after this".

    The subscription opens before the turn starts and closes on every exit path,
    so nothing is missed at the front and nothing is left registered at the back.
    Only events published after this point arrive: continuing an old session does
    not repaint its history.
    """

    display = _start_display(
        runtime, console, session, timeline=timeline,
        heartbeat_seconds=heartbeat_seconds, clock=clock,
    )

    prepared: ProductChatTurn | None = None
    task_input = text
    if product is not None:
        prepared = await product.prepare_turn(session.session_id, text)
        task_input = prepared.turn_input

    # Shielded so an interrupt delivered here cannot detach the turn: the
    # runtime keeps tracking it and can still cancel it through its own
    # cancellation path.
    turn = asyncio.ensure_future(
        runtime.run_existing(session.session_id, task_input)
    )
    try:
        result = await asyncio.shield(turn)
    except asyncio.CancelledError:
        # The interesting case, and the one that was previously broken. The
        # cancellation lifecycle - runtime/cancel-requested, the cancelled model
        # attempt, step/end and turn/end - is only appended *while*
        # `runtime.cancel()` runs, so tearing the display down first published
        # all of it to nobody. The display therefore stays open across
        # convergence and is drained afterwards.
        interrupted = await _interrupt_turn(
            runtime, console, session, display, turn
        )
        if product is not None:
            await product.discard_turn(session.session_id, None)
        return interrupted
    except Exception as error:
        # AgentLoop already recorded runtime/error and closed the lifecycle.
        # Draining first keeps the timeline that led up to the failure - it is
        # the most useful part of it - ahead of the error line.
        await _stop_display(display)
        if product is not None:
            await product.discard_turn(session.session_id, None)
        console.write(f"error: {type(error).__name__}: {error}")
        return False
    except BaseException:
        await _stop_display(display)
        raise
    await _stop_display(display)
    _write_turn_result(console, result)
    if product is not None:
        assert prepared is not None
        await product.finish_turn(
            session.session_id,
            prepared,
            turn_id=result.turn_id,
            console=console,
            heartbeat_seconds=heartbeat_seconds,
            clock=clock,
        )
    return False


def _write_turn_result(console: Console, result) -> None:
    console.write(f"{ASSISTANT_PREFIX}{result.final_text}")
    console.write(
        f"[reason={result.reason} steps={result.steps} "
        f"tokens={result.usage.total_tokens} verification={result.verification_passed}]"
    )


async def _interrupt_turn(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    display: _TurnDisplay,
    turn: asyncio.Future,
) -> bool:
    """Converge an interrupted turn with its narration still running.

    Order matters and is the whole fix:

    1. the display stays open, so cancellation events are actually observed;
    2. ``runtime.cancel()`` appends `runtime/cancel-requested`, cancels the turn
       and waits for the model, tools and subprocesses to converge;
    3. only then is the display drained, so those events reach the console
       before the notice;
    4. the chat returns to its prompt with the session intact.

    Repeated interrupts cannot shorten step 2. Convergence is delegated to the
    shared `await_worker_convergence()`, which absorbs further cancellation and
    keeps waiting for the same future rather than offering an early exit. A
    second Ctrl+C is honoured *after* convergence, by leaving.
    """

    task = asyncio.current_task()
    interrupts_before = task.cancelling() if task is not None else 0

    convergence = asyncio.ensure_future(
        runtime.cancel(session.session_id, reason="interrupted from the chat console")
    )
    await await_worker_convergence(convergence)

    interrupts_after = task.cancelling() if task is not None else 0
    # Clear our own cancellation state; the interrupt has been handled here and
    # the loop is allowed to keep running. Without this the task stays flagged as
    # cancelling and the next await would abort the session the user just kept.
    if task is not None:
        while task.uncancel() > 0:
            pass

    # Retrieve the turn's outcome so a cancelled or failed future cannot surface
    # later as a never-retrieved exception.
    outcome = (await asyncio.gather(turn, return_exceptions=True))[0]

    await _stop_display(display)

    if not isinstance(outcome, BaseException):
        # The turn finished in the gap between the interrupt and the cancel; its
        # result is real, so report it instead of claiming an interruption.
        _write_turn_result(console, outcome)
    else:
        console.write(INTERRUPTED_TURN_NOTICE)
    return interrupts_after > interrupts_before


def _start_display(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    *,
    timeline: bool,
    heartbeat_seconds: float,
    clock: Clock,
) -> _TurnDisplay:
    """Subscribe and start the narration tasks for one turn."""

    if not timeline:
        return _TurnDisplay()
    subscription = runtime.events.subscribe(
        runtime.sessions.session_stream(session.session_id)
    )
    tracker = ActivityTracker(interval_seconds=heartbeat_seconds, monotonic=clock.monotonic)
    printer = asyncio.create_task(
        _print_timeline(subscription, console, tracker),
        name="traceh-chat-timeline",
    )
    heartbeat = (
        asyncio.create_task(
            _emit_heartbeat(console, tracker, clock),
            name="traceh-chat-heartbeat",
        )
        if tracker.enabled
        else None
    )
    return _TurnDisplay(subscription=subscription, printer=printer, heartbeat=heartbeat)


async def _print_timeline(
    subscription: EventSubscription,
    console: Console,
    tracker: ActivityTracker,
) -> None:
    """Write one line per shown event, for as long as the subscription is open."""

    renderer = TimelineRenderer()
    async for event in subscription:
        elapsed = tracker.observe(event)
        line = renderer.render(event, elapsed_seconds=elapsed)
        if line is not None:
            console.write(line)


async def _emit_heartbeat(console: Console, tracker: ActivityTracker, clock: Clock) -> None:
    """Report waiting time while an activity is in flight.

    Time-driven rather than event-driven, which is the point: the silence being
    filled is precisely the absence of events. Nothing is written when nothing is
    in flight, so a fast turn produces no heartbeat at all.

    Each wake is scheduled from the *activity's* next threshold, not from a fixed
    tick of its own. Sleeping one interval at a time would phase-lock the
    heartbeat to whenever the turn started: a tool beginning at t=10.1 with a 10s
    interval would be silent at the t=20 wake (only 9.9s elapsed) and first
    reported at t=30, almost twenty seconds into the wait. With nothing in flight
    there is no deadline to aim at, so it waits one interval and re-checks - which
    is also what puts it in phase with an activity that starts meanwhile.
    """

    while True:
        delay = tracker.seconds_until_next_wait()
        if delay is None:
            await clock.sleep(tracker.interval_seconds)
        elif delay > 0:
            await clock.sleep(delay)
        # A due threshold is printed without sleeping first; this cannot spin,
        # because emitting a line advances that activity past the threshold.
        for line in tracker.due_waits():
            console.write(line)


async def _stop_display(display: _TurnDisplay) -> None:
    """Tear the narration down as a unit, leaving no task behind."""

    if display.heartbeat is not None:
        display.heartbeat.cancel()
        await await_worker_convergence(display.heartbeat)
    await _drain_timeline(display.subscription, display.printer)


async def _drain_timeline(
    subscription: EventSubscription | None,
    printer: asyncio.Task[None] | None,
) -> None:
    """Close the subscription and let the printer finish what is already queued.

    Closing enqueues an end marker behind the events published so far, so
    awaiting the printer prints all of them and then returns. That ordering is
    what lets the caller promise the timeline appears before the final answer,
    without polling or sleeping.

    Cancellation converges rather than abandons. ``asyncio.shield`` alone would
    not be enough: it protects the printer from being cancelled, but it does not
    keep *this* coroutine waiting, so a cancelled drain would return while the
    printer was still writing to the console - the same detached-worker shape
    already fixed for store and provider workers. So a `CancelledError` is
    caught, the printer is awaited to completion through the shared
    `await_worker_convergence()` (which absorbs repeated cancellation instead of
    treating it as an early exit), and only then is the original cancellation
    re-raised. The chat's cancellation is never swallowed.

    A printer that raises is a display bug, not a turn outcome: the exception is
    dropped here so it can neither fail a turn that succeeded nor mask the
    exception or cancellation the caller is already handling.
    """

    if subscription is not None:
        subscription.close()
    if printer is None:
        return
    try:
        await asyncio.shield(printer)
    except asyncio.CancelledError as cancellation:
        await await_worker_convergence(printer)
        raise cancellation
    except Exception:
        # An observer must not change what the runtime reported.
        pass


async def _converge_interrupted(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    resume_environment: ResumeEnvironment,
) -> None:
    console.write("")
    await runtime.cancel(session.session_id, reason="interrupted from the chat console")
    await _write_resume_block_for_session(runtime, console, session, resume_environment)


async def _write_session_banner(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    resume_environment: ResumeEnvironment | None = None,
) -> None:
    console.write(
        f"session_id={session.session_id} workspace={session.workspace} "
        f"provider={runtime.config.provider} model={runtime.config.model}"
    )
    await _write_resume_block_for_session(runtime, console, session, resume_environment)


async def _write_resume_block_for_session(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    resume_environment: ResumeEnvironment | None = None,
) -> None:
    """Print a resume hint from the Session's durable plugin identity."""

    try:
        plugin_ids = await runtime.persisted_external_plugin_ids(session.session_id)
    except Exception:
        # A failed or malformed identity read must not turn the current
        # Generation into a false recovery instruction.  Keep only inert,
        # escaped locator data and omit the command until the Session can be
        # inspected safely.
        _write_resume_block(
            runtime,
            console,
            session,
            resume_environment,
            plugin_ids=(),
            omit_command=True,
        )
        return
    _write_resume_block(
        runtime,
        console,
        session,
        resume_environment,
        plugin_ids=plugin_ids,
    )


def _write_resume_block(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    resume_environment: ResumeEnvironment | None = None,
    *,
    shell: str | None = None,
    plugin_ids: Sequence[str],
    omit_command: bool = False,
) -> None:
    """Print a copy-pasteable way back into this session.

    Printed at startup rather than only on exit, and that is the design: a hard
    interrupt - Ctrl+Break, closing the console - runs no Python at all, so an
    exit-time hint can never be relied on. Printing it up front means the
    information is already in the user's scroll-back whatever happens next.

    The block is split along the line that actually matters to a reader:

    * **finding the session** needs the session id and the resolved absolute data
      dir - the store lives under it, so a session started with a custom
      ``--data-dir`` or from another working directory cannot be reopened without
      it;
    * **restoring how it behaved** needs provider, model and the rest, because
      those may have come from a `.env` in the original working directory. A
      command carrying only the session id would re-resolve them wherever it runs
      and silently continue on a different model.

    It is deliberately **not** a complete configuration snapshot, and says so
    when it withholds something. Two values are never echoed verbatim:

    * ``--verify-command`` is arbitrary shell text. There is no way to show it and
      also promise it holds no credential, so it is omitted; when the env file
      supplied it, reloading that file restores it, and otherwise the user is told
      to re-supply it.
    * a base URL is withheld when it structurally carries credentials - see
      `_safe_base_url()`.

    Every value is placed in an argv token list and rendered once by
    `cli/command_line.py`, so shell metacharacters are quoted by that shell's own
    rule rather than by a shared guess.
    """

    environment = resume_environment or ResumeEnvironment()
    config = runtime.config
    data_dir = Path(config.data_dir).resolve()
    shell_name = shell or default_shell()

    if omit_command:
        console.write("resume later:")
        console.write(f"  session_id: {escape_for_display(session.session_id)}")
        console.write(f"  data dir:   {escape_for_display(str(data_dir))}")
        console.write(
            "  note: the Session's durable plugin identity could not be read safely; "
            "no resume command was printed."
        )
        return

    # Fixed literals stay bare so the command is runnable and readable; every
    # derived value goes through the renderer's quoting rule.
    locate: list[str] = [
        Literal("traceh"),
        Literal("chat"),
        Literal("--session-id"),
        session.session_id,
        Literal("--data-dir"),
        str(data_dir),
    ]
    restore: list[str] = [
        Literal("--provider"),
        str(config.provider),
        Literal("--model"),
        str(config.model),
    ]
    notes: list[str] = []

    if config.max_steps:
        restore += [Literal("--max-steps"), str(config.max_steps)]

    if config.verifier_name is not None:
        restore += [Literal("--plugin-verifier"), config.verifier_name]

    # Without these the resumed session would be refused by the plugin identity
    # check - correctly, but with no hint about what to re-enable.
    for plugin_id in plugin_ids:
        restore += [Literal("--plugin"), plugin_id]

    if environment.script is not None:
        restore += [Literal("--script"), str(Path(environment.script).resolve())]
        notes.append(
            "  note: --script replays the same file from its first response; "
            "the scripted cursor is not persisted across processes."
        )

    if environment.product_config is not None:
        restore += [
            Literal("--product-config"),
            str(Path(environment.product_config).resolve()),
        ]

    base_url, withheld_reason = _safe_base_url(environment.base_url)
    if base_url:
        restore += [Literal("--base-url"), base_url]
    elif withheld_reason:
        notes.append(
            f"  note: --base-url omitted because {withheld_reason}; re-supply it manually."
        )

    # The key variable only matters to a provider that sends one. Naming
    # OPENAI_API_KEY for a scripted session would be noise at best and a
    # misleading instruction at worst.
    key_name = environment.api_key_env
    if key_name and config.provider != "scripted" and is_env_var_name(key_name):
        restore += [Literal("--api-key-env"), key_name]
        if key_name in environment.env_file_supplies:
            notes.append(
                f"  note: {key_name} must be available from that env file or the shell; "
                "its value is never printed."
            )
        else:
            notes.append(
                f"  note: set {key_name} in that shell; its value is never printed."
            )

    if environment.env_file is not None:
        restore += [Literal("--env-file"), str(Path(environment.env_file).resolve())]

    if config.verification_command:
        # The flag, not the presence of the key: an explicit --verify-command
        # overrides the file, so a file containing that key proves nothing about
        # which value is actually in effect.
        if environment.verifier_from_env_file and environment.env_file is not None:
            notes.append(
                "  note: the verifier command is restored by the env file above, "
                "not shown here."
            )
        else:
            notes.append(
                "  note: Verifier command omitted from the displayed resume command; "
                "re-supply it manually."
            )

    try:
        resume_line = render_command(locate + restore, shell=shell_name)
        sessions_line = render_command(
            [Literal("traceh"), Literal("sessions"), Literal("--data-dir"), str(data_dir)],
            shell=shell_name,
        )
    except UnsafeCommandValue:
        # A value that cannot be rendered as one line is not printed as a command
        # at all; the ids are still shown so the session remains findable.
        # Escaped, not raw: the value that made rendering impossible is exactly
        # the value that would otherwise break this explanation too. A data dir
        # containing a newline previously produced a second terminal line here.
        console.write("resume later:")
        console.write(f"  session_id: {escape_for_display(session.session_id)}")
        console.write(f"  data dir:   {escape_for_display(str(data_dir))}")
        console.write(
            "  note: a configuration value contains characters that cannot be shown "
            "safely on one command line, so no command is printed. The values above "
            "are escaped for display."
        )
        return

    label = "PowerShell" if shell_name == "powershell" else "POSIX shell"
    console.write(f"resume later ({label}):")
    console.write(f"  {resume_line}")
    console.write(f"  {sessions_line}")
    for note in notes:
        console.write(note)
    console.write(
        "  note: this restores the session and its non-secret settings; "
        "it is not a complete configuration snapshot."
    )


def _write_seq_note(console: Console, *, timeline: bool) -> None:
    """Explain the event numbers once, before any of them appear."""

    if not timeline:
        return
    for line in _SEQ_NOTE_LINES:
        console.write(line)
