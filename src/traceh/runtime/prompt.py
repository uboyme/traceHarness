"""Composable prompt sections with deterministic assembly."""

from __future__ import annotations

from traceh.api.prompts import PromptSection
from traceh.kernel.lifespan import CallbackRegistration


class PromptAssembler:
    def __init__(self, sections: tuple[PromptSection, ...] = ()) -> None:
        self._sections = list(sections)

    def register(self, section: PromptSection) -> CallbackRegistration:
        """Register a section and return the registration that removes it again."""

        if any(item.section_id == section.section_id for item in self._sections):
            raise RuntimeError(f"prompt section already registered: {section.section_id}")
        self._sections.append(section)

        async def cleanup() -> None:
            for index, current in enumerate(self._sections):
                if current is section:
                    self._sections.pop(index)
                    return

        return CallbackRegistration(cleanup)

    def section_ids(self) -> tuple[str, ...]:
        return tuple(sorted(section.section_id for section in self._sections))

    def sections(self) -> tuple[PromptSection, ...]:
        return tuple(sorted(self._sections, key=lambda item: (item.priority, item.section_id)))

    def assemble(self, *, workspace: str) -> str:
        runtime_section = PromptSection(
            "traceh.runtime.workspace",
            f"Workspace root: {workspace}\n"
            "All file and process operations must stay in this workspace.",
            50,
        )
        sections = sorted(
            (*self._sections, runtime_section),
            key=lambda item: (item.priority, item.section_id),
        )
        return "\n\n".join(
            f"## {section.section_id}\n{section.content.strip()}" for section in sections
        )

    def fork(self) -> PromptAssembler:
        """Return an independent registration surface with borrowed sections."""

        return PromptAssembler(self.sections())


def default_coding_prompt() -> PromptAssembler:
    return PromptAssembler(
        (
            PromptSection(
                "traceh.identity",
                "You are a coding agent running inside TraceHarness. Work incrementally "
                "and use tools "
                "to inspect the repository before changing it.",
                10,
            ),
            PromptSection(
                "traceh.execution",
                "Do not claim success from intuition. Run the relevant tests or checks. "
                "When a tool "
                "fails, inspect its structured output and choose the next action.",
                20,
            ),
            PromptSection(
                "traceh.tools",
                "Use apply_patch for exact, reviewable file edits. The shell tool does not "
                "invoke a "
                "system shell; pass a normal command string that can be split into argv.",
                30,
            ),
        )
    )


__all__ = ["PromptAssembler", "PromptSection", "default_coding_prompt"]
