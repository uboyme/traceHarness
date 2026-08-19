"""Provider registry kept outside the AgentLoop."""

from __future__ import annotations

from traceh.api.llm import LlmProvider


class LlmRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LlmProvider] = {}
        self._composition_resource_binding = None

    def register(self, provider: LlmProvider, *, replace: bool = False) -> None:
        provider_binding = getattr(provider, "_composition_resource_binding", None)
        if (
            self._composition_resource_binding is not None
            and provider_binding is not None
            and self._composition_resource_binding is not provider_binding
        ):
            raise ValueError("LLM registry mixes composition resource lineages")
        if self._composition_resource_binding is None and provider_binding is not None:
            self._composition_resource_binding = provider_binding
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

    def fork(self) -> LlmRegistry:
        """Return an independent provider registry with borrowed providers."""

        forked = LlmRegistry()
        forked._providers = dict(self._providers)
        forked._composition_resource_binding = self._composition_resource_binding
        return forked
