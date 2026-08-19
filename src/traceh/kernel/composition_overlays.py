"""Four-layer Tool, Prompt and Policy composition overlays.

This module resolves assembly-time bindings into the existing ToolRegistry,
PromptAssembler and ToolRuntime policy tuple.  It deliberately does not expose
a second runtime or a mutable published hierarchy: the resolved values are fed
into one CompositionGeneration, which remains the only Step-visible fact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from traceh.api.prompts import PromptSection
from traceh.api.tools import EffectKind, Tool
from traceh.kernel.scope import ScopeKind
from traceh.tools.policy import ToolPolicy
from traceh.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from traceh.runtime.prompt import PromptAssembler

class _NamedScopedBinding(Protocol):
    @property
    def name(self) -> str: ...

    scope: ScopeKind
    replace: bool

_SCOPE_ORDER = (
    ScopeKind.APPLICATION,
    ScopeKind.WORKSPACE,
    ScopeKind.PRESET,
    ScopeKind.AGENT,
)


class CompositionOverlayConflictError(RuntimeError):
    """A deterministic named-capability conflict between scope layers."""

    def __init__(
        self,
        code: str,
        *,
        capability: str,
        name: str,
        scope: str,
        existing_scope: str,
    ) -> None:
        self.code = code
        self.capability = capability
        self.name = name
        self.scope = scope
        self.existing_scope = existing_scope
        super().__init__(
            f"{code}: cannot bind {capability} in {scope}; "
            f"existing binding is in {existing_scope}"
        )


def _validate_scope_and_replace(scope: ScopeKind, replace: bool, *, kind: str) -> None:
    if not isinstance(scope, ScopeKind):
        raise TypeError(f"scoped {kind} binding scope must be a ScopeKind")
    if not isinstance(replace, bool):
        raise TypeError(f"scoped {kind} binding replace must be a bool")


@dataclass(frozen=True, slots=True)
class ScopedToolBinding:
    """One borrowed Tool targeted at an explicit composition scope."""

    scope: ScopeKind
    tool: Tool
    replace: bool = False

    def __post_init__(self) -> None:
        _validate_scope_and_replace(self.scope, self.replace, kind="tool")
        name = getattr(self.tool, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scoped tool binding tool name must be a non-blank string")
        if not isinstance(getattr(self.tool, "description", None), str):
            raise TypeError("scoped tool binding description must be a string")
        if not isinstance(getattr(self.tool, "input_schema", None), dict):
            raise TypeError("scoped tool binding input_schema must be an object")
        if not isinstance(getattr(self.tool, "effect_kind", None), EffectKind):
            raise TypeError("scoped tool binding effect_kind must be an EffectKind")
        if not callable(getattr(self.tool, "execute", None)):
            raise TypeError("scoped tool binding execute must be callable")

    @property
    def name(self) -> str:
        return self.tool.name


@dataclass(frozen=True, slots=True)
class ScopedPromptBinding:
    """One borrowed PromptSection targeted at an explicit composition scope."""

    scope: ScopeKind
    section: PromptSection
    replace: bool = False

    def __post_init__(self) -> None:
        _validate_scope_and_replace(self.scope, self.replace, kind="prompt")
        if not isinstance(self.section, PromptSection):
            raise TypeError("scoped prompt binding section must be a PromptSection")
        if (
            not isinstance(self.section.section_id, str)
            or not self.section.section_id.strip()
        ):
            raise ValueError("scoped prompt binding section_id must be a non-blank string")
        if not isinstance(self.section.content, str):
            raise TypeError("scoped prompt binding content must be a string")
        if not isinstance(self.section.priority, int) or isinstance(
            self.section.priority, bool
        ):
            raise TypeError("scoped prompt binding priority must be an integer")

    @property
    def name(self) -> str:
        return self.section.section_id


@dataclass(frozen=True, slots=True)
class ScopedPolicyBinding:
    """One borrowed ToolPolicy targeted at an explicit composition scope."""

    scope: ScopeKind
    policy: ToolPolicy
    replace: bool = False

    def __post_init__(self) -> None:
        _validate_scope_and_replace(self.scope, self.replace, kind="policy")
        name = getattr(self.policy, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scoped policy binding name must be a non-blank string")
        if not callable(getattr(self.policy, "check", None)):
            raise TypeError("scoped policy binding check must be callable")

    @property
    def name(self) -> str:
        return self.policy.name


@dataclass(frozen=True, slots=True)
class ResolvedCompositionOverlay:
    """The one effective composition that may enter a Generation."""

    tools: ToolRegistry
    prompt: PromptAssembler
    policies: tuple[ToolPolicy, ...]


def _ordered[BindingT: _NamedScopedBinding](
    bindings: tuple[BindingT, ...],
) -> tuple[BindingT, ...]:
    return tuple(
        item
        for _, item in sorted(
            enumerate(bindings),
            key=lambda pair: (_SCOPE_ORDER.index(pair[1].scope), pair[0]),
        )
    )


def _resolve_named[T, BindingT: _NamedScopedBinding](
    base: tuple[tuple[str, T], ...],
    bindings: tuple[BindingT, ...],
    *,
    capability: str,
    value_of: Callable[[BindingT], T],
) -> tuple[tuple[str, T], ...]:
    values: dict[str, T] = {}
    sources: dict[str, ScopeKind] = {}
    for name, value in base:
        if name in values:
            raise CompositionOverlayConflictError(
                f"{capability}-already-bound",
                capability=capability,
                name=name,
                scope=ScopeKind.APPLICATION.value,
                existing_scope=ScopeKind.APPLICATION.value,
            )
        values[name] = value
        sources[name] = ScopeKind.APPLICATION

    for binding in _ordered(bindings):
        name = binding.name
        scope = binding.scope
        existing_scope = sources.get(name)
        if existing_scope is not None:
            if not binding.replace:
                suffix = (
                    "already-bound"
                    if existing_scope is scope
                    else "override-requires-replace"
                )
                raise CompositionOverlayConflictError(
                    f"{capability}-{suffix}",
                    capability=capability,
                    name=name,
                    scope=scope.value,
                    existing_scope=existing_scope.value,
                )
        values[name] = value_of(binding)
        sources[name] = scope
    return tuple(values.items())


@dataclass(frozen=True, slots=True)
class CompositionOverlayPlan:
    """Immutable assembly blueprint for the three model-affecting overlays."""

    tool_bindings: tuple[ScopedToolBinding, ...] = ()
    prompt_bindings: tuple[ScopedPromptBinding, ...] = ()
    policy_bindings: tuple[ScopedPolicyBinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_bindings", tuple(self.tool_bindings))
        object.__setattr__(self, "prompt_bindings", tuple(self.prompt_bindings))
        object.__setattr__(self, "policy_bindings", tuple(self.policy_bindings))
        if any(not isinstance(item, ScopedToolBinding) for item in self.tool_bindings):
            raise TypeError("tool_bindings must contain ScopedToolBinding values")
        if any(
            not isinstance(item, ScopedPromptBinding) for item in self.prompt_bindings
        ):
            raise TypeError("prompt_bindings must contain ScopedPromptBinding values")
        if any(
            not isinstance(item, ScopedPolicyBinding) for item in self.policy_bindings
        ):
            raise TypeError("policy_bindings must contain ScopedPolicyBinding values")

    def application_only(self) -> CompositionOverlayPlan:
        return self._matching(lambda scope: scope is ScopeKind.APPLICATION)

    def children_only(self) -> CompositionOverlayPlan:
        return self._matching(lambda scope: scope is not ScopeKind.APPLICATION)

    def _matching(
        self,
        predicate: Callable[[ScopeKind], bool],
    ) -> CompositionOverlayPlan:
        return CompositionOverlayPlan(
            tuple(item for item in self.tool_bindings if predicate(item.scope)),
            tuple(item for item in self.prompt_bindings if predicate(item.scope)),
            tuple(item for item in self.policy_bindings if predicate(item.scope)),
        )

    def resolve(
        self,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        policies: tuple[ToolPolicy, ...],
    ) -> ResolvedCompositionOverlay:
        """Resolve against private inputs without mutating any caller-owned object."""

        tool_entries = _resolve_named(
            tuple((name, tools.require(name)) for name in tools.names()),
            self.tool_bindings,
            capability="tool",
            value_of=lambda binding: binding.tool,
        )
        resolved_tools = tools.fork()
        for name, tool in tool_entries:
            if resolved_tools.get(name) is not tool:
                resolved_tools.register(
                    tool,
                    replace=resolved_tools.get(name) is not None,
                )

        prompt_entries = _resolve_named(
            tuple((section.section_id, section) for section in prompt.sections()),
            self.prompt_bindings,
            capability="prompt",
            value_of=lambda binding: binding.section,
        )
        resolved_prompt = prompt.fork()
        current_sections = {
            section.section_id: section for section in resolved_prompt.sections()
        }
        for name, section in prompt_entries:
            if current_sections.get(name) is not section:
                resolved_prompt.register(
                    section,
                    replace=name in current_sections,
                )
                current_sections[name] = section

        if self.policy_bindings:
            policy_entries = _resolve_named(
                tuple((policy.name, policy) for policy in policies),
                self.policy_bindings,
                capability="policy",
                value_of=lambda binding: binding.policy,
            )
            resolved_policies = tuple(policy for _, policy in policy_entries)
        else:
            # Preserve the legacy ability to provide multiple policies with the
            # same display name when no scoped identity lookup is requested.
            resolved_policies = tuple(policies)

        return ResolvedCompositionOverlay(
            resolved_tools,
            resolved_prompt,
            resolved_policies,
        )


__all__ = [
    "CompositionOverlayConflictError",
    "CompositionOverlayPlan",
    "ResolvedCompositionOverlay",
    "ScopedPolicyBinding",
    "ScopedPromptBinding",
    "ScopedToolBinding",
]
