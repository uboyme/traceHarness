# ADR-003: Kernel protocol is not replaceable by plugins

**Status:** accepted

Sequence, lifecycle closure, ownership, Scope resolution and registration disposal are
correctness rules. Providers, tools, prompts and policies are replaceable; the rules that
make their composition safe are not.
