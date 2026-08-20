"""Provider registry kept outside the AgentLoop."""

from __future__ import annotations

from traceh.api.llm import LlmProvider
from traceh.kernel.lifespan import CallbackRegistration


class LlmRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LlmProvider] = {}
        self._composition_resource_binding = None

    def register(
        self,
        provider: LlmProvider,
        *,
        replace: bool = False,
    ) -> CallbackRegistration:
        """Register a provider and return an idempotent reversal handle."""

        if not isinstance(replace, bool):
            raise TypeError("LLM provider replace must be a bool")
        provider_binding = getattr(provider, "_composition_resource_binding", None)
        if (
            self._composition_resource_binding is not None
            and provider_binding is not None
            and self._composition_resource_binding is not provider_binding
        ):
            raise ValueError("LLM registry mixes composition resource lineages")
        if self._composition_resource_binding is None and provider_binding is not None:
            self._composition_resource_binding = provider_binding
        name = provider.name
        previous = self._providers.get(name)
        if previous is not None and not replace:
            raise RuntimeError(f"LLM provider already registered: {name}")
        self._providers[name] = provider

        async def cleanup() -> None:
            current = self._providers.get(name)
            if current is provider:
                if previous is None:
                    self._providers.pop(name, None)
                else:
                    self._providers[name] = previous

        return CallbackRegistration(cleanup)

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
