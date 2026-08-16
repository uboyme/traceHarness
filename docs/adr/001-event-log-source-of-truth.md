# ADR-001: Event Log is the source of truth

**Status:** accepted

Mutable `messages` and `state` objects cannot explain what happened before a crash or
what the model actually saw. TraceHarness therefore persists facts first and derives
runtime state and model Surface through projectors. Caches may be added later but are not
authoritative.
