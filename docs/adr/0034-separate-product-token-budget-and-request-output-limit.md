# ADR-0034: Separate Product Token Budget from the request output limit

- Status: Accepted; implementation in v0.7-F5 release stabilization
- Date: 2026-08-28
- Stage: v0.7-F5

## Context

`BudgetLimits.max_tokens` is cumulative authority. The Budget ledger charges
input plus output usage across every model call made by one Agent and closes the
account only after its lifecycle converges.

`ModelRequest.max_output_tokens` is different. It is the maximum output one
provider request may produce. Providers apply their own request ceilings, while
a multi-step coding Agent needs a cumulative account larger than any one
response.

The Product Profile previously named only the cumulative Budget. When the
runtime request had no explicit output bound, `BudgetedLlmRuntime` correctly
used the remaining Token authority as its conservative request bound. That
fallback is sound for a generic Budget wrapper, but it made one Product field do
two jobs. A real provider refused role accounts of 60000/120000 as per-request
limits because its endpoint accepted at most 32768. Reducing the accounts to
32768 made requests legal but caused a real multi-step Coder to exhaust its
whole cumulative account after eleven successful responses. Neither result was
a model-quality observation.

The distinction belongs in the host-frozen Product Profile. It does not belong
in a provider-specific clamp, the Agent loop or a guessed default.

## Decision

### 1. Every Product role and Router states both limits explicitly

`ProductRoleProfile` and `ProductRouterProfile` each require a positive integer
`max_output_tokens` in addition to their `BudgetLimits`.

- `budget.max_tokens` is cumulative Agent authority charged by the existing
  append-only Budget ledger.
- `max_output_tokens` is the requested output ceiling for each model call.

Both values participate in the computed Product Profile digest. The exact
schema-1 host and benchmark readers require the new key; the older shape is
rejected. There is no alias, default, compatibility reader or migration.

### 2. Product resource binding carries the request bound to the existing Runtime

The Product resource binding pairs the already resolved assembly and Budget
with the Profile's request output bound. `ProductAgentRuntimeFactory` passes it
to the existing `RuntimeConfig.max_output_tokens`. No Product code changes the
provider payload directly.

The Budget enforcement rule remains unchanged. For a call it uses the smaller
of the explicit request output limit and the account's remaining Token
authority, then settles the existing reservation from durable usage. The
request bound therefore cannot mint capacity or bypass the ledger.

### 3. Benchmark arms share one frozen pair of limits

The shipped Product benchmark uses the same Profile for single, multi and auto:

- parent/reviewer cumulative Token Budget: 60000 each;
- coder cumulative Token Budget: 120000;
- each role request output limit: 8192;
- Router cumulative Token Budget: 8000 and request output limit: 256;
- aggregate ProductTask Token Budget: 500000.

These are explicit benchmark conditions, not library defaults and not values
selected from a provider name. Auto continues to differ only by its Router call
and resolved fixed topology.

### 4. Historical acceptance remains historical

The earlier fifth RC grid remains durable evidence for the Profile it actually
ran. It cannot certify the changed Profile digest or the new request bounds.
Before v0.7 release, the 18-attempt real-provider grid must be run again under
the new exact manifest; the report must preserve provider failures and quality
outcomes without retry or fallback.

## Consequences

- A provider request no longer inherits an Agent's whole cumulative allowance
  merely because Product configuration omitted a request bound.
- A multi-step Agent may spend more than one response ceiling over its whole
  lifecycle while the ledger still enforces its cumulative limit.
- Profile and assembly preflight comparison detects a changed request bound.
- Chat and benchmark hosts use the same parser and runtime path, so the split
  cannot drift between interactive and measured ProductTasks.
- `AgentLoop`, `AgentRuntime`, `ProcessAgentSupervisor`, provider adapters and
  the Budget ledger gain no Product-specific branch.

## Rejected alternatives

- **Clamp by provider or model name.** That is hidden example hardcoding, goes
  stale as endpoints change and makes two hosts with the same Profile behave
  differently.
- **Keep 32768 as both limits.** Legal requests can still exhaust a legitimate
  multi-step Agent's cumulative account prematurely.
- **Let the provider silently clamp an oversized request.** The durable Profile
  would no longer describe what ran, and provider implementations could differ.
- **Add a default request bound.** A missing host decision would silently become
  library policy and would not be visible in the manifest being compared.
- **Change Budget reservation or usage settlement.** The ledger already owns
  cumulative authority correctly; weakening it would solve the wrong problem.
- **Add retry/fallback.** Repeating a failed call changes cost and experiment
  conditions and does not distinguish the two meanings of Token limit.

## Explicit boundaries

This ADR does not add a provider, retry policy, adaptive output sizing, tokenizer
guess, dynamic Budget increase, new Workflow state or model-controlled
configuration. It does not certify model quality. The v0.7 release remains
blocked on independent review, the post-change acceptance grid, final gates,
packaging, versioning and explicit release authorization.
