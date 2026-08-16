"""Provider registry kept outside the AgentLoop."""

from __future__ import annotations

from traceh.api.llm import LlmProvider


class LlmRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LlmProvider] = {}

    def register(self, provider: LlmProvider, *, replace: bool = False) -> None:
        if provider.name in self._providers and not replace:
            raise RuntimeError(f"LLM provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def get(self, name: str) -> LlmProvider | None:
        return self._providers.get(name)

    def require(self, name: str) -> LlmProvider:
        provider = self.get(name)
        if provider is None:
            raise LookupError(f"unknown LLM provider: {name}")
        return provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
