# ADR-002: Separate Session, Turn, Step and Model Attempt

**Status:** accepted

A Session persists across tasks and restarts. A Turn handles one external wake-up. A
Step is one logical model decision plus its tools. A Model Attempt is one provider HTTP
request. Provider retry can therefore add Attempts without changing Step semantics.

On cancellation, `AgentLoop` owns one finalization Task for the currently open lifecycle.
It freshly resolves whether the current Attempt start became durable, then closes any open
Attempt, Step and Turn in that order. Repeated cancellation affects only the waiter and is
absorbed until this same finalizer converges; an independent finalizer failure remains visible
alongside the original cancellation. A public Turn therefore cannot return while its durable
terminal facts are still being appended in the background.
