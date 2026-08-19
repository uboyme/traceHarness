"""TraceHarness micro-kernel primitives."""

from traceh.kernel.composition import CompositionSnapshot, RuntimeComposition
from traceh.kernel.composition_overlays import (
    CompositionOverlayConflictError,
    ScopedPolicyBinding,
    ScopedPromptBinding,
    ScopedToolBinding,
)
from traceh.kernel.scope import Scope, ScopeChain, ScopedServiceBinding, ScopeKind

__all__ = [
    "CompositionSnapshot",
    "CompositionOverlayConflictError",
    "RuntimeComposition",
    "Scope",
    "ScopeChain",
    "ScopeKind",
    "ScopedServiceBinding",
    "ScopedPolicyBinding",
    "ScopedPromptBinding",
    "ScopedToolBinding",
]
