"""What a Turn is started from.

`AgentLoop.run_turn()` historically took a bare ``str`` and minted its own
``message_id``, stamping ``source="user"``. That is fine when the caller *is*
the user typing at a prompt, but it makes the message unaddressable: a control
plane that already recorded "this message was accepted, and here is its id" had
no way to say "run *that* one", and afterwards no way to prove which Session
Turn corresponded to which durable message except by comparing text.

`TurnInput` is the smallest thing that closes that gap. It is deliberately
generic - an id, some content and where it came from - and knows nothing about
Agents, Inboxes, Supervisors or delivery. `AgentLoop` accepts it without
importing any of those, which is what keeps the loop free of control-plane
concepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

DEFAULT_TURN_SOURCE = "user"
"""Source recorded for a plain ``str`` task, matching the historical behaviour."""


@dataclass(frozen=True, slots=True)
class TurnInput:
    """One message a Turn is started from, with a caller-chosen identity.

    ``message_id`` is what makes the Turn addressable. Whatever supplies it -
    a CLI, an evaluator, a supervisor - can later find the Session Turn that
    ran this exact message, because the same id appears in ``inbox/accepted``,
    ``inbox/claimed`` and ``turn/start``.
    """

    content: str
    message_id: str
    source: str = DEFAULT_TURN_SOURCE

    @classmethod
    def from_task(cls, task: str | TurnInput) -> TurnInput:
        """Normalize either accepted form into a `TurnInput`.

        A plain ``str`` keeps exactly the previous behaviour - a fresh id and
        ``source="user"`` - so every existing caller is unaffected.
        """

        if isinstance(task, cls):
            return task
        return cls(content=task, message_id=str(uuid4()), source=DEFAULT_TURN_SOURCE)


__all__ = ["DEFAULT_TURN_SOURCE", "TurnInput"]
