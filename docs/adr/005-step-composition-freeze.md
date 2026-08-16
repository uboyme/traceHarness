# ADR-005: Freeze Composition per Step

**Status:** accepted

One Step must not assemble a prompt with one tool generation and execute a different
generation. v0.3 creates an immutable Composition Snapshot. Future hot updates will use
generation leases but retain this guarantee.
