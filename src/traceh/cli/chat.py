"""Interactive multi-turn chat loop.

The loop owns no conversation state. Every turn goes through
``AgentRuntime.run_existing()``, so the Session event log stays the single
source of truth and the model history is projected from it, exactly as for the
one-shot commands. What lives here is only terminal behaviour: prompting,
internal commands, rendering a turn result and converging on exit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from traceh.cli.console import Console, contains_undecodable_input, normalize_input
from traceh.cli.errors import CliConfigurationError
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.service import SessionNotFoundError

PROMPT = "you> "
ASSISTANT_PREFIX = "assistant> "

#: Conventional shell exit code for "terminated by SIGINT".
INTERRUPTED_EXIT_CODE = 130

_HELP_LINES = (
    "/help     show these commands",
    "/session  show the current session id, workspace, provider and model",
    "/exit     leave the chat",
    "/quit     leave the chat",
)


@dataclass(frozen=True, slots=True)
class ChatSession:
    session_id: str
    workspace: Path


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
) -> int:
    """Open or continue a session, then read turns until the user leaves."""

    workspace, session_id = chat_target(workspace, session_id)
    try:
        session = await _open_session(
            runtime, console, workspace=workspace, session_id=session_id
        )
        try:
            return await _chat_loop(runtime, console, session)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C reaches us either as KeyboardInterrupt raised while reading
            # a line, or as cancellation of the turn that was running. This is
            # the top-level coroutine of the process, so turning both into an
            # exit code is the whole job rather than swallowing someone's
            # cancellation.
            await _converge_interrupted(runtime, console, session)
            return INTERRUPTED_EXIT_CODE
    finally:
        await runtime.dispose()


async def _open_session(
    runtime: AgentRuntime,
    console: Console,
    *,
    workspace: Path | None,
    session_id: str | None,
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
        _write_session_banner(runtime, console, session)
        return session

    try:
        resolved_workspace = await runtime.sessions.workspace_for(session_id)
    except SessionNotFoundError as error:
        raise CliConfigurationError(f"session not found: {session_id}") from error
    session = ChatSession(session_id, resolved_workspace)

    # Converge whatever the previous process left open before the user can add
    # anything new. No turn is started and no instruction is injected: the next
    # turn is the one the user types.
    report = await runtime.recovery.recover(session_id)
    _write_session_banner(runtime, console, session)
    if report.changed:
        console.write(
            "recovered: "
            f"model_attempts={report.closed_model_attempts} "
            f"tool_results={report.synthesized_tool_results} "
            f"step={report.closed_step} turn={report.closed_turn}"
        )
    return session


async def _chat_loop(runtime: AgentRuntime, console: Console, session: ChatSession) -> int:
    console.write("Type /help for commands, /exit to leave.")
    while True:
        try:
            line = console.read_line(PROMPT)
        except EOFError:
            console.write("")
            return 0

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
            if _handle_command(runtime, console, session, text):
                return 0
            continue
        await _run_turn(runtime, console, session, text)


def _handle_command(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    text: str,
) -> bool:
    """Handle one internal command. Returns True when the chat should end."""

    if text in {"/exit", "/quit"}:
        return True
    if text == "/help":
        for entry in _HELP_LINES:
            console.write(entry)
        return False
    if text == "/session":
        _write_session_banner(runtime, console, session)
        return False
    console.write(f"unknown command: {text} (try /help)")
    return False


async def _run_turn(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
    text: str,
) -> None:
    # Shielded so an interrupt delivered here cannot detach the turn: the
    # runtime keeps tracking it and can still cancel it through its own
    # cancellation path.
    turn = asyncio.ensure_future(runtime.run_existing(session.session_id, text))
    try:
        result = await asyncio.shield(turn)
    except Exception as error:
        # AgentLoop already recorded runtime/error and closed the lifecycle.
        console.write(f"error: {type(error).__name__}: {error}")
        return
    console.write(f"{ASSISTANT_PREFIX}{result.final_text}")
    console.write(
        f"[reason={result.reason} steps={result.steps} "
        f"tokens={result.usage.total_tokens} verification={result.verification_passed}]"
    )


async def _converge_interrupted(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
) -> None:
    console.write("")
    await runtime.cancel(session.session_id, reason="interrupted from the chat console")
    console.write(f"interrupted. resume with: traceh chat --session-id {session.session_id}")


def _write_session_banner(
    runtime: AgentRuntime,
    console: Console,
    session: ChatSession,
) -> None:
    console.write(
        f"session_id={session.session_id} workspace={session.workspace} "
        f"provider={runtime.config.provider} model={runtime.config.model}"
    )
