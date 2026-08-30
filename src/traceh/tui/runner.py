"""Lazy optional-dependency boundary and lifecycle owner for Textual Chat."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

from traceh.chat.activity import Clock, default_clock
from traceh.chat.session import open_chat_session
from traceh.cli.errors import CliConfigurationError
from traceh.concurrency import combine_failures
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.service import SessionNotFoundError

if TYPE_CHECKING:
    from traceh.product.host import ProductChatHost

TUI_INSTALL_HINT = (
    "Textual TUI is not installed; install the optional extra with "
    "'python -m pip install traceharness-py[tui]'"
)


def require_textual() -> None:
    """Reject a missing optional adapter without silently falling back to Line."""

    if importlib.util.find_spec("textual") is None:
        raise CliConfigurationError(TUI_INSTALL_HINT)


async def run_tui(
    runtime: AgentRuntime,
    *,
    workspace: Path | None,
    session_id: str | None,
    timeline: bool,
    heartbeat_seconds: float,
    product: ProductChatHost | None = None,
    clock: Clock | None = None,
) -> int:
    """Open the shared Session, run Textual, then converge shared owners."""

    require_textual()
    primary: BaseException | None = None
    result: int | None = None
    try:
        try:
            opened = await open_chat_session(
                runtime,
                workspace=workspace,
                session_id=session_id,
            )
        except NotADirectoryError as error:
            raise CliConfigurationError(
                f"workspace is not a directory: {error}"
            ) from error
        except SessionNotFoundError as error:
            raise CliConfigurationError(f"session not found: {session_id}") from error
        from traceh.tui.app import TracehTuiApp

        app = TracehTuiApp(
            runtime,
            opened,
            timeline=timeline,
            heartbeat_seconds=heartbeat_seconds,
            product=product,
            clock=clock or default_clock(),
        )
        app_result = await app.run_async()
        result = 0 if app_result is None else app_result
    except BaseException as error:
        primary = error
    finally:
        cleanup: BaseException | None = None
        if product is not None:
            try:
                await product.aclose()
            except BaseException as error:
                cleanup = error
        try:
            await runtime.dispose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "TUI runtime shutdown failed")
        combined = combine_failures(primary, cleanup, "TUI shutdown failed")
        if combined is not None:
            raise combined
    assert result is not None
    return result


__all__ = ["TUI_INSTALL_HINT", "require_textual", "run_tui"]
