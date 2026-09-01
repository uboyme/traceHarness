"""Turn Session events into one-line terminal activity.

This is a presentation projection and nothing else: it reads an `EventEnvelope`
and returns text, never writes, never persists, and never reaches the model. It
lives under `cli/` because it is terminal wording; `AgentLoop` has no idea it
exists.

Three rules shape every line here.

**The number is the fact.** Each line carries the event's real persisted `seq`
in its own stream, labelled ``event`` so it cannot be mistaken for a file line
number or a counter the CLI invented. Anything shown can be found again in the
event log.

**Show the shape, not the contents.** A timeline exists to say what the agent is
doing, so it names steps, tools and outcomes - and deliberately omits prompts,
request snapshots, composition snapshots, assistant text, file contents,
patches, command lines and command output. Unknown event types render as nothing
rather than as a raw payload dump: a display that prints whatever it does not
recognise is how secrets end up on a terminal.

**Every payload string is untrusted.** A tool name, a provider name or an error
type reaches this module from a model response, a tool argument or an exception
message. Rendered raw, a newline forges an extra timeline row and an ESC byte
becomes a live terminal control sequence. So no payload string is interpolated
directly: every one goes through `sanitize()`, and `payload_text()` is the only way a
handler reads one.
"""

from __future__ import annotations

import re

from traceh.api.events import EventEnvelope
from traceh.cli.text_safety import is_unsafe_character

#: Upper bound for any payload-derived fragment. Long values are cut rather than
#: wrapped, because one event must stay one line.
MAX_DETAIL_CHARS = 60

#: Argument allowed to be summarised for each known tool. A tool absent from
#: this table shows its name and call id only - the conservative default, since
#: an unknown tool's arguments have unknown sensitivity.
#:
#: ``shell`` is deliberately absent. A command line is the most likely place for
#: a credential to appear, and no keyword scan can be trusted to recognise every
#: shape a secret takes; scanning for a few words and printing the rest is a leak
#: waiting for an unusual token format. A shell call shows its name and call id,
#: and nothing about what it runs.
_TOOL_DETAIL_ARGUMENT = {
    "list_files": "path",
    "read_file": "path",
    "search_text": "query",
    "apply_patch": "path",
}

#: Words that suggest a credential, plus shapes that are one. Both suppress a
#: detail entirely rather than scrubbing part of it: a partially redacted secret
#: is still a leak, and a timeline is never the right place to find out. This is
#: a backstop for values already restricted to workspace paths - not a general
#: secret detector, which is why `shell` is excluded above instead of scanned.
_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|credential|authorization|bearer"
    r"|sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[abpsr]-[A-Za-z0-9-]{8,}"
    r"|://[^/\s:@]+:[^/\s@]+@)"
)


def sanitize(value: str, *, limit: int = MAX_DETAIL_CHARS) -> str:
    """Make one untrusted payload string safe to place on a terminal line.

    Control, format and line-separator characters become spaces, which
    neutralises them without pretending to interpret them: an ESC sequence loses
    its ESC and the remaining bracket text is inert, a newline can no longer forge
    a row, and a bidirectional override can no longer reorder what the user reads.
    The set comes from `cli/text_safety.py`, so `U+2028`/`U+2029` are covered here
    for the same reason they are refused in a command - they are line breaks to
    enough consumers to split one row into two. Whitespace then collapses so the
    result is exactly one line, and the length is bounded so a long value cannot
    push real information off screen.
    """

    scrubbed = "".join(
        " " if is_unsafe_character(character) else character for character in value
    )
    flat = " ".join(scrubbed.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def payload_text(data: dict, key: str) -> str | None:
    """Read one payload string, sanitized, or ``None`` when unusable.

    The single funnel for payload text: a handler that wants a field calls this
    and therefore cannot forget to sanitize it. A non-string, or a value that is
    empty once scrubbed, counts as absent rather than being coerced.
    """

    value = data.get(key)
    if not isinstance(value, str):
        return None
    return sanitize(value) or None


def _tool_detail(data: dict) -> str:
    """A short, safe description of what a tool call is aimed at.

    Returns an empty string whenever safety is not obvious. The fallback is not
    silence about the call - the tool name and call id are always shown by the
    caller - it is silence about the argument.
    """

    name = payload_text(data, "tool_name")
    if name is None:
        return ""
    argument = _TOOL_DETAIL_ARGUMENT.get(name)
    if argument is None:
        return ""
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        return ""
    value = arguments.get(argument)
    if not isinstance(value, str) or not value.strip():
        return ""
    # Checked before sanitizing: scrubbing first could split a credential shape
    # across the space it inserts and let the pattern miss it.
    if _SENSITIVE.search(value):
        return ""
    safe = sanitize(value)
    return f" {safe}" if safe else ""


def _call_reference(data: dict) -> str:
    """Identify the call when its target cannot be shown."""

    call_id = payload_text(data, "tool_call_id")
    return f" (call {call_id})" if call_id else ""


class TimelineRenderer:
    """Renders events to lines, remembering only what display needs.

    It keeps a step-id to step-number map so a step can be closed with the same
    number it opened with, because ``step/end`` does not carry the number. That
    is ephemeral display memory, scoped to one subscription: it is not a
    projection anyone may query, not persisted, and never a second source of
    truth. Rebuilding state from events is `StateProjector`'s job.
    """

    def __init__(self) -> None:
        self._step_numbers: dict[str, int] = {}

    def render(self, event: EventEnvelope, *, elapsed_seconds: float | None = None) -> str | None:
        """One display line, or ``None`` when this event is not shown.

        Never raises on a surprising payload: a missing or wrongly typed field
        degrades the line, and an unrecognised type is skipped. A chat session
        must not die because an event carried less than expected.

        ``elapsed_seconds`` appends how long the finished activity took. It is
        measured by `cli/activity.py` from a monotonic clock, not read from the
        payload and not derived from event timestamps, so it stays a display
        annotation rather than a claim about persisted data.
        """

        body = self._body(event)
        if body is None:
            return None
        suffix = ""
        if elapsed_seconds is not None and elapsed_seconds >= 0:
            suffix = f" ({elapsed_seconds:.1f}s)"
        return f"[event {event.seq}] {body}{suffix}"

    def _body(self, event: EventEnvelope) -> str | None:
        data = event.data if isinstance(event.data, dict) else {}
        handler = _HANDLERS.get(event.type)
        if handler is None:
            return None
        return handler(self, data)

    def _step_label(self, data: dict) -> str:
        step_id = payload_text(data, "step_id")
        number = self._step_numbers.get(step_id) if step_id else None
        return f"Step {number}" if number is not None else "Step"

    # -- handlers ---------------------------------------------------------
    # Each returns the text after the event number, or None to show nothing.

    def _turn_start(self, data: dict) -> str:
        return "Turn started"

    def _turn_end(self, data: dict) -> str:
        reason = payload_text(data, "reason") or "ended"
        return f"Turn ended ({reason})"

    def _step_start(self, data: dict) -> str:
        step_id = payload_text(data, "step_id")
        number = data.get("number")
        if step_id and isinstance(number, int) and not isinstance(number, bool):
            self._step_numbers[step_id] = number
        return f"{self._step_label(data)} started"

    def _step_end(self, data: dict) -> str:
        label = self._step_label(data)
        reason = payload_text(data, "reason")
        if reason in (None, "model_response"):
            return f"{label} completed"
        return f"{label} ended ({reason})"

    def _attempt_start(self, data: dict) -> str:
        provider = payload_text(data, "provider")
        model = payload_text(data, "model")
        if provider and model:
            return f"Model {provider}/{model} called"
        return "Model called"

    def _attempt_end(self, data: dict) -> str | None:
        status = payload_text(data, "status")
        if status == "succeeded":
            return "Model responded"
        if status is None:
            return "Model attempt ended"
        failure_code = payload_text(data, "failure_code")
        failure_category = payload_text(data, "failure_category")
        if failure_code:
            suffix = f" / {failure_category}" if failure_category else ""
            return f"Model attempt {status} ({failure_code}{suffix})"
        error_type = payload_text(data, "error_type")
        if error_type:
            return f"Model attempt {status} ({error_type})"
        return f"Model attempt {status}"

    def _tool_call(self, data: dict) -> str:
        name = payload_text(data, "tool_name") or "unknown tool"
        detail = _tool_detail(data) or _call_reference(data)
        return f"Tool {name} requested{detail}"

    def _tool_admitted(self, data: dict) -> str:
        name = payload_text(data, "tool_name") or "unknown tool"
        return f"Tool {name} started"

    def _tool_result(self, data: dict) -> str:
        name = payload_text(data, "tool_name") or "unknown tool"
        status = payload_text(data, "status") or "finished"
        error_type = payload_text(data, "error_type")
        if status != "succeeded" and error_type:
            return f"Tool {name} {status} ({error_type})"
        return f"Tool {name} {status}"

    def _verification(self, data: dict) -> str:
        passed = data.get("passed")
        if passed is True:
            return "Verification passed"
        exit_code = data.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return f"Verification failed (exit_code={exit_code})"
        return "Verification failed"

    def _runtime_error(self, data: dict) -> str:
        # Only the exception type. A message is arbitrary text from an arbitrary
        # exception - a provider error can quote the request, an auth failure can
        # quote the credential it tried - so it is not shown here at all. Chat
        # prints its own error line from the exception it caught; the timeline
        # does not repeat that text. The traceback in this payload is never shown.
        return f"Runtime error: {payload_text(data, 'error_type') or 'error'}"

    def _cancel_requested(self, data: dict) -> str:
        return "Cancellation requested"

    def _recovered(self, data: dict) -> str:
        parts = []
        for key, label in (
            ("closed_model_attempts", "model_attempts"),
            ("synthesized_tool_results", "tool_results"),
        ):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                parts.append(f"{label}={value}")
        suffix = f" ({' '.join(parts)})" if parts else ""
        return f"Recovered{suffix}"


#: Only these types produce output. Everything else - `composition/snapshot`,
#: `request/snapshot`, `assistant/*`, `user/message`, `inbox/*`,
#: `session/created`, `surface/replace`, every effect event and anything added
#: later - renders as nothing until someone decides how to show it safely.
_HANDLERS = {
    "turn/start": TimelineRenderer._turn_start,
    "turn/end": TimelineRenderer._turn_end,
    "step/start": TimelineRenderer._step_start,
    "step/end": TimelineRenderer._step_end,
    "model/attempt-start": TimelineRenderer._attempt_start,
    "model/attempt-end": TimelineRenderer._attempt_end,
    "tool/call": TimelineRenderer._tool_call,
    "tool/admitted": TimelineRenderer._tool_admitted,
    "tool/result": TimelineRenderer._tool_result,
    "verification/result": TimelineRenderer._verification,
    "runtime/error": TimelineRenderer._runtime_error,
    "runtime/cancel-requested": TimelineRenderer._cancel_requested,
    "runtime/recovered": TimelineRenderer._recovered,
}
