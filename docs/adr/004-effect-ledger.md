# ADR-004: Tool Calls need an Effect Ledger

**Status:** accepted

`tool/call` and `tool/result` leave an ambiguous interval in which the world changed but
the process died before persisting a result. Separate Effect Intent and Outcome events
make that interval observable and allow domain reconcilers without blindly retrying
writes.
