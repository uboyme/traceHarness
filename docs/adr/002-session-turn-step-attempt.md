# ADR-002: Separate Session, Turn, Step and Model Attempt

**Status:** accepted

A Session persists across tasks and restarts. A Turn handles one external wake-up. A
Step is one logical model decision plus its tools. A Model Attempt is one provider HTTP
request. Provider retry can therefore add Attempts without changing Step semantics.
