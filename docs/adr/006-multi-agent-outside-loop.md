# ADR-006: Multi-agent orchestration stays outside AgentLoop

**Status:** accepted

Agent lifecycle, Inbox, ownership, budgets and workspace isolation belong to a
Supervisor and Workflow layer. Subagent creation is exposed as a tool. This prevents the
single-Agent loop from accumulating mode-specific branches.
