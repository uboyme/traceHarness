# ADR-0038: Schema-gated multiline Tool argument normalization

- Status: Accepted and implemented
- Date: 2026-08-31
- Stage: v0.8-F5 release-candidate repair

## Context

ADR-0037 correctly made malformed Provider responses non-retryable protocol
failures. During the real v0.8 TUI acceptance flow, however, two independent
ProductTasks reached the same repeatable failure after the coder had read the
source and tests. Durable Session facts proved an HTTP Provider dispatch ended
as `provider-response-invalid`, before any `apply_patch` Effect existed.

A sanitized replay of the exact frozen request reproduced the leaf response:
the OpenAI-compatible endpoint returned HTTP 200 and selected `apply_patch`,
but encoded the top-level multiline `old_text` and `new_text` string values with
double Python-style triple-quote delimiters. The surrounding object otherwise
used JSON syntax. Strict `json.loads(function.arguments)` therefore failed at
the first triple-quoted value. The raw response body, arguments and credentials
were not persisted or printed.

This is not a reason to retry a protocol failure, switch Provider/model, weaken
the frozen request identity, or accept arbitrary JSON-like text. It is one
observed lexical dialect at the Provider response boundary. Supporting it must
remain narrower than a general repair parser and must be bound to the exact
Tool schema already present in the admitted request.

## Decision

### 1. Strict JSON remains the first and normal path

The OpenAI-compatible adapter first parses every string-valued
`function.arguments` with the standard library JSON decoder configured to
reject Python's non-JSON `NaN`/`Infinity` constants. A JSON object is accepted
exactly as before. Mapping-valued arguments remain accepted for
compatible endpoints that return the already-decoded object.

Only a JSON syntax error or rejected non-JSON numeric constant may enter the
bounded normalization below; the latter cannot pass unless normalization also
produces strict JSON. The outer response uses the same strict constant rule.
Transport, HTTP, Usage, Tool-call identity and scalar-shape validation are
unchanged.

### 2. The one accepted lexical extension is schema-gated

The adapter resolves the returned Tool name against the exact `request.tools`
tuple. It tokenizes the argument text and may replace a double triple-quoted
value only when all of these facts hold:

- the whole argument is intended to be a top-level object;
- the value belongs directly to that object, not to a nested object or array;
- the property exists in the matched Tool's frozen JSON Schema and has
  `type: string`;
- the delimiter is exactly `"""`; single-quoted strings and prefixed Python
  literals are not accepted;
- the block contents themselves form a valid JSON string interior;
- after replacement with a canonical JSON string, the complete argument text
  passes the normal JSON decoder and produces an object.

The tokenizer prevents a regular expression from confusing escaped triple
quotes inside source text with the outer delimiter. It does not evaluate the
argument object or execute Python syntax. One response may normalize several
top-level string properties when every property independently satisfies the
same rule.

### 3. Every other malformed argument still fails closed

Unknown Tools or properties, a non-string schema property, nested triple-quote
values, single-quoted Python mappings, comments, trailing commas, expressions,
`NaN`, positive/negative `Infinity`, unterminated delimiters and any remainder
that is not strict JSON are rejected
as `provider-tool-arguments-invalid` in the existing `protocol` category. The
stable code contains no raw body, argument fragment, secret or path and remains
non-retryable under ADR-0037.

Normalization is response-side only. It does not change the admitted
`ModelRequest`, request fingerprint, Provider/model identity, Session dispatch
permit, Budget reservation, Tool schema validation, Tool policy, Effect ledger
or Workspace authority.

## Rejected alternatives

### Rely only on a stronger prompt or Tool description

Rejected as the root fix because the Tool schema already says these values are
strings and model compliance is probabilistic. Guidance may reduce incidence
but cannot prove that a paid response will be parseable.

### Use `eval`, `ast.literal_eval`, JSON5 or a general JSON repair package

Rejected because those approaches accept a much larger language: Python
booleans and containers, comments, single quotes, expressions or other syntax
that the admitted Tool schema did not authorize. The implemented normalizer
interprets only the contents of a schema-declared triple-quoted string as a JSON
string interior; the surrounding result must still be strict JSON.

### Retry protocol failures or fall back to another Provider/model

Rejected for the reasons in ADR-0037. Repeating or changing the paid call does
not make the first malformed response a valid durable Attempt, and fallback
would change execution identity.

### Hard-code `qwen-plus`, `apply_patch` or the inventory demo

Rejected because the rule is a Provider-boundary syntax decision. Eligibility
comes only from the returned Tool name and the frozen request Tool schema. The
production implementation contains no model, fixture, filename or task-name
special case.

### Change the `apply_patch` Tool contract to avoid multiline strings

Rejected because the Tool's exact-text replacement contract is useful and is
not the source of the malformed wire syntax. Replacing it with a second Tool or
parallel schema would spread one Provider dialect into Runtime and Tool owners.

## Consequences and verification

- Normal compliant Providers continue through the original strict JSON path.
- The observed multiline response can reach the existing Tool admission and
  Effect owners without weakening those owners.
- A malformed argument outside the one closed rule still fails once as a typed
  protocol error, with unknown Usage conservatively settled by Budget.
- Deterministic HTTP tests cover the accepted form, escaped triple quotes in
  source text, twelve rejected neighboring forms (including direct and
  normalized non-finite constants) and a public
  `AgentRuntime + ApplyPatchTool` execution that changes a temporary file and
  records paired Tool facts.
- Removing the normalizer makes the accepted counter-example fail before any
  Tool Effect; restoring it makes the same public path complete.
- A direct, proxy-free replay of the real frozen request was accepted as one
  `apply_patch` Tool call with exact Usage. A subsequent fresh real TUI
  ProductTask completed through Review, typed Approval and one-shot Promotion.
  No response body, arguments, `.env` contents or Key were retained.
