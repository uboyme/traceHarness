# ADR-0037: Typed Provider failures and bounded same-request retry

- Status: Accepted and implemented; Release Stop B complete
- Date: 2026-08-29
- Stage: v0.8-F2

## Context

The v0.7 ProductTask acceptance runs separated model quality failures from
transport failures only by inspecting provider exception text after the fact.
That is not a safe runtime policy: HTTP bodies, headers and transport exception
messages may contain credentials or local paths, and string matching cannot
reliably distinguish a temporary DNS failure from authentication, malformed
requests or an invalid response.

ADR-0035 made Session CAS the sole dispatch permit and gave every Provider call
an Attempt-scoped Budget reservation. F2 may now retry a temporary Provider
failure without inventing another Runner or loop, but only if every later call
is represented as another Attempt in the same Step and uses the exact
provider-bound request frozen by ordinal one.

## Decision

### 1. Provider adapters own stable, sanitized failure classification

`ProviderFailure` carries only a stable kebab-case code, one
`ProviderFailureCategory`, an optional finite numeric Retry-After hint and
optional typed Usage. Its public string is the code. Raw response bodies,
headers and transport exception text never cross this boundary.

The OpenAI-compatible adapter uses this fixed classification:

| Result | Category | Retry candidate |
|---|---|---|
| 401 | authentication | no |
| 403 | permission | no |
| 400/404/405/409/413/414/415/422 | invalid request | no |
| invalid URL/configuration | configuration | no |
| invalid JSON or response shape | protocol | no |
| temporary DNS resolution | dns | yes |
| connect/read timeout and HTTP 408 | timeout | yes |
| TLS premature EOF | tls_eof | yes |
| reset/abort/broken pipe/remote disconnect/incomplete read | disconnected | yes |
| 429 | rate_limited | yes |
| 500/502/503/504 | server_transient | yes |
| every unrecognized HTTP/transport/Provider failure | unknown | no |

The adapter is stdlib `urllib` and has no hidden SDK retry. A Provider or plugin
that raises an untyped exception is reduced by `LlmAdmission` to the stable
non-retryable `provider-failure-unclassified`; subclassed failures are reduced
the same way. `runtime/error`, Timeline and durable Attempt evidence therefore
receive only sanitized fields for Provider failures. Non-Provider diagnostics
retain their existing bounded traceback contract.

Response parsing is strict at the adapter boundary: a missing choice message,
an explicitly non-mapping Usage value, malformed Tool calls or wrong scalar
types are protocol failures. Only an absent (or empty) Usage object means the
Provider supplied no trustworthy count and therefore produces
`UsageQuality.UNKNOWN`.

### 2. The host owns one explicit bounded policy

`ModelRetryPolicy` names the maximum Attempt count, total retry-window elapsed,
base and maximum delay, Retry-After cap, jitter ratio and a subset of the fixed
candidate categories. A host may narrow the candidate set but cannot add
authentication, permission, invalid-request, configuration, protocol or
unknown failures. All numeric bounds are finite. `max_attempts=1` is the
explicit no-retry policy.

CLI `run`, `resume`, `chat` and `eval` resolve the same six explicit arguments
or environment variables. The shipped CLI defaults are three total Attempts,
30 seconds total retry-window elapsed, 0.5 second base delay, 4 second per-delay
cap, 8 second Retry-After cap and 0.2 jitter. Programmatic Runtime construction
defaults to `NO_MODEL_RETRY`; a composition root must opt in. Product roles and
Router receive the same host policy, and one Evaluation run records and applies
one policy across every task, repetition and arm.

`RetryScheduler` supplies monotonic time, sleep and entropy. Production uses
the standard monotonic clock, `asyncio.sleep` and local entropy; tests inject all
three and never guess timing with arbitrary sleeps. Exponential backoff uses a
bounded floating-point exponent operation and applies the finite host cap on
overflow; every policy accepted by the public constructor therefore still
returns a finite delay at a very large valid ordinal.

### 3. Retry remains inside the existing Step and Attempt protocol

`AgentLoop` builds the request once. Ordinal one admits any Budget-shaped final
request and freezes it in the existing request snapshot. A retry obtains a new
host Attempt identity and calls the same `LlmRuntime.admit()` path with the
frozen request. Both AgentLoop and Session CAS reject any later request or
fingerprint drift before dispatch. Provider and model are still bound to the
same Composition lease and concrete admission from ADR-0035.

Each failed Attempt end records status, stable failure code/category, optional
sanitized Usage and Provider-active milliseconds. A later Attempt start records
its own ordinal plus the preceding failure code/category and the measured retry
wait; Session CAS and core invariants require that binding to the immediately
preceding failed Attempt. Under the same Stream lock, Session also runs the
shared `CoreInvariantChecker` over the fresh existing history before granting
any later dispatch permit. An already-invalid history fails closed as an
ownership conflict; duplicate facts are not folded into authority, repaired or
deleted. There is still one request snapshot and one final model response for
the Step.

Strict Product Router parsing is not a Provider failure and is never retried.
Recovery only closes an open Attempt; it has no scheduler or authority to
create a later ordinal or call a Provider.

### 4. Budget and cancellation remain per Attempt and owner-convergent

Every ordinal independently reserves, starts, charges and settles the exact
same request. Known failure Usage is settled by its quality rules; missing or
untrusted Usage consumes the full reservation conservatively. If the remaining
balance cannot reserve the frozen request, the retry is refused rather than
lowering `max_output_tokens`.

Cancellation during delay, reservation, Attempt start, Provider work, Attempt
end or settlement cannot admit a later Attempt. Stateful Budget finalizers
converge first, then cancellation is returned to AgentLoop so its existing
Attempt -> Step -> Turn owner can close durable evidence. A prior Provider
failure must not wrap that cancellation in a `BaseExceptionGroup` and bypass
the lifecycle owner.

### 5. Reports distinguish reliability cost from model quality

Session facts now expose Attempt count, retry wait, Provider-active time,
failure categories and each Session's final model result. Reliable Usage from
failed Attempts contributes to Provider-reported execution Tokens; missing or
unknown failure Usage makes that metric unavailable, while the Budget Ledger
continues to show its conservative settlement.

`traceh eval` records the policy and reports these fields for routing and
execution in both JSON and Markdown. Retry does not change Product success,
resolved-arm attribution or experiment conditions. A retry-assisted success is
a reliability outcome, not evidence that the model became better and not a new
quality arm.

## Rejected alternatives

### Retry inside the Provider adapter or `LlmRuntime.dispatch()`

Rejected because Session and Budget would observe one Attempt while multiple
paid calls occurred. The adapter classifies; the existing Step owner decides
whether another durable Attempt may exist.

### Parse exception strings or persist raw Provider diagnostics

Rejected because text is neither a stable classification nor a safe evidence
format. It can contain secrets, paths, request fragments and provider-specific
wording.

### Retry with a smaller request, another model or another Provider

Rejected because that changes the experimental and execution identity. A
smaller request is not a cheaper retry of the frozen call; model/provider
fallback is a separate future decision and is absent from F2.

### Let recovery continue the retry sequence

Rejected because a crash leaves no live owner that can prove elapsed policy,
current cancellation or external-call outcome. Recovery remains append-only
closure, never replay.

### Add a retry Event stream or a second Benchmark Runner

Rejected because Attempt start/end, Budget reservations and existing reports
already own every fact. Another state machine would create competing truth.

## Consequences and explicit boundaries

- Temporary failures may produce several paid Attempts in one Step, but never
  an unrecorded Provider call.
- A later Attempt cannot be authorized from Session history that already
  violates a core invariant, even when individual duplicate events are
  otherwise canonical.
- Backoff contributes to Step/wall time but not Provider-active time.
- Unknown Usage remains unknown in model metrics and conservative in Budget;
  it is never written as an estimated zero.
- The policy does not retry Tool, Workflow, Verification, strict Router parse,
  EventStore busy/CAS, recovery or Promotion failures.
- F2 adds no Provider/model fallback, proxy or TLS weakening, second Runner,
  TUI/driver, Skill/Memory, sandbox, version bump or release action.
- Release Stop B independent review cleared P0/P1/P2. The complete F1+F2 suite
  then passed with `2472 passed, 7 skipped` and exit code 0; the slowest L2
  isolation validation took `1097.00s`. F2 is complete and adds none of the
  later-stage capabilities listed above.
