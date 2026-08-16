"""Composable prompt sections with deterministic assembly."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptSection:
    section_id: str
    content: str
    priority: int = 100


class PromptAssembler:
    def __init__(self, sections: tuple[PromptSection, ...] = ()) -> None:
        self._sections = list(sections)

    def register(self, section: PromptSection) -> None:
        if any(item.section_id == section.section_id for item in self._sections):
            raise RuntimeError(f"prompt section already registered: {section.section_id}")
        self._sections.append(section)

    def assemble(self, *, workspace: str) -> str:
        runtime_section = PromptSection(
            "traceh.runtime.workspace",
            f"Workspace root: {workspace}\nAll file and process operations must stay in this workspace.",
            50,
        )
        sections = sorted((*self._sections, runtime_section), key=lambda item: (item.priority, item.section_id))
        return "\n\n".join(f"## {section.section_id}\n{section.content.strip()}" for section in sections)


def default_coding_prompt() -> PromptAssembler:
    return PromptAssembler(
        (
            PromptSection(
                "traceh.identity",
                "You are a coding agent running inside TraceHarness. Work incrementally and use tools "
                "to inspect the repository before changing it.",
                10,
            ),
            PromptSection(
                "traceh.execution",
                "Do not claim success from intuition. Run the relevant tests or checks. When a tool "
                "fails, inspect its structured output and choose the next action.",
                20,
            ),
            PromptSection(
                "traceh.tools",
                "Use apply_patch for exact, reviewable file edits. The shell tool does not invoke a "
                "system shell; pass a normal command string that can be split into argv.",
                30,
            ),
        )
    )
