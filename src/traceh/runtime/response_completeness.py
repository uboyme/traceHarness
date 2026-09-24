"""One shared reading of whether a model response finished what it started.

A provider call that returned HTTP 200 proves the network worked, not that the
Agent produced anything.  A response cut off by the output ceiling can still
carry parseable tool calls, and those calls are a fragment of an intention the
model never got to finish stating - executing them treats half a sentence as an
authorisation.  So the judgment lives here, is made once per response, and is
consumed by both the loop (which decides whether tools may run) and the
continuation policy (which decides how the Turn ends).  Neither re-derives it,
and Product/Supervisor do not keep private copies of the rule.

What this module deliberately does not decide is whether a *delivery* is good
enough: that an investigator owes a report, or a patch author owes a patch, is
a role contract owned by Product.  A generic Session cannot tell business
success from a polite sentence, and should not try.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.llm import CompletionCategory, ModelResponse

#: Stable Turn-end reasons. They are codes, not prose, because evaluators and
#: the Product success chain match on them.
TRUNCATED = "response-truncated"
REFUSED = "response-refused"
COMPLETION_UNKNOWN = "response-completion-unknown"
TOOL_HANDOFF_WITHOUT_CALLS = "response-tool-handoff-without-calls"


@dataclass(frozen=True, slots=True)
class ResponseCompleteness:
    """Whether this one response may drive side effects and may end a Turn well.

    ``reason`` is empty exactly when ``complete`` is true, so a caller cannot
    accidentally report a healthy response under a failure code.
    """

    complete: bool
    may_execute_tools: bool
    reason: str = ""


_COMPLETE = ResponseCompleteness(complete=True, may_execute_tools=True)


def judge_response(response: ModelResponse) -> ResponseCompleteness:
    """Classify one response before any of its tool calls are dispatched."""

    completion = response.completion
    if completion is CompletionCategory.LENGTH:
        # Partial content is kept by the caller as evidence; it is just not a
        # delivery. Any tool calls in a truncated response stay unexecuted.
        return ResponseCompleteness(False, False, TRUNCATED)
    if completion is CompletionCategory.REFUSAL:
        return ResponseCompleteness(False, False, REFUSED)
    if completion is CompletionCategory.UNKNOWN:
        # The provider did not tell us how this ended, so we have not been shown
        # that it ended well. Guessing here is what turned an empty answer into
        # a completed Turn before.
        return ResponseCompleteness(False, False, COMPLETION_UNKNOWN)
    if completion is CompletionCategory.TOOL_HANDOFF and not response.tool_calls:
        # The provider said it was handing tools back and then handed none.
        # Treating that as an ordinary finish is how an empty response becomes
        # a success; the contradiction is reported instead of resolved.
        return ResponseCompleteness(False, False, TOOL_HANDOFF_WITHOUT_CALLS)
    return _COMPLETE
