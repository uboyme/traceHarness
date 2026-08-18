"""Stable prompt contribution value types.

``PromptSection`` lives in ``traceh.api`` rather than in ``traceh.runtime.prompt``
because plugins contribute sections through the public SDK, and the SDK must not
have to import the runtime assembly layer to name a value type.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One deterministic section of the model system prompt.

    Sections are ordered by ``(priority, section_id)`` so the assembled prompt -
    and therefore the Composition revision and Request fingerprint - does not
    depend on registration order.
    """

    section_id: str
    content: str
    priority: int = 100


__all__ = ["PromptSection"]
