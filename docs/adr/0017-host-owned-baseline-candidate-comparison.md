# ADR-0017: Host-owned baseline/candidate comparison

## Context

ADR-0016 proves that one exact candidate Wheel can be built, audited, installed, tested and
regression-checked against one explicit core commit. Passing those gates does not prove that the
plugin improves the capability it claims to add. Candidate-authored tests and self-assessment
cannot decide that question, and comparison logic does not belong in `AgentRuntime`, `AgentLoop`,
`PluginManager` or a second plugin loader.

## Decision

- L3 remains a development control plane under `traceh.evolution`, exposed by
  `traceh plugins compare`. It consumes an existing successful L2 evidence directory and never
  rebuilds the candidate.
- The L2 report must use schema version 1, name the canonical ordered validation gates, report all
  of them passed, identify one core commit and one plugin, and bind one Wheel by filename, size and
  SHA-256. The Wheel must be the artifact under that evidence bundle and must pass the same archive
  audit again before comparison.
- The caller supplies an explicit trusted core Git repository, a relative suite path inside that
  repository, a new output directory and an explicit dependency source. The comparator clones the
  exact commit recorded by L2; dirty core files and an arbitrary external task directory cannot
  change the evaluator.
- The suite manifest and every task asset are host-owned, bounded and copied without symbolic
  links, Windows Junctions or other reparse points. It fixes the task, scripted model responses,
  baseline verifier, candidate verifier, expected completion evidence and per-case budgets.
- Dependencies are resolved exactly once into a bounded all-Wheel set. Every Wheel is recorded by
  filename, size and SHA-256; sdists are rejected. Baseline and candidate then install offline from
  that same frozen set, and their installed Distribution receipts must match before either arm runs
  and remain unchanged afterwards. The baseline leaves the target plugin disabled; only the
  candidate arm enables the exact L2 plugin identity.
- A host-owned probe drives the existing `build_default_runtime_async`, `AgentLoop`, Session Event
  Log, Effect ledger and Verifier paths. It records bounded facts from durable events: case result,
  Step/model/tool counts, non-success Tool Results, Verification, invariants, request
  reconstruction and duration. A normal method return is not completion evidence: the matching
  durable `turn/end` must exist, its reason and Step count must agree with the return value, no Turn
  or Step may remain open, and every in-Turn `composition/snapshot` must record the expected arm
  plugin identity. Missing lifecycle evidence and identity mismatches are explicit failure codes.
- Candidate code never supplies the comparator or success function. After candidate execution the
  host rechecks the original L2 report bytes, Wheel bytes/audit/SHA-256, frozen dependency set,
  installed receipts and both suite-copy digests. The frozen local Wheel policy is inherited by
  nested Tool and Verifier subprocesses as one canonical percent-encoded local `file://` URI.
  Raw paths, whitespace-separated values, remote hosts, query strings and fragments are rejected;
  index URLs remain excluded.
- L3 classifies only `improved`, `regressed`, `mixed` or `no-change`. It writes JSON and Markdown as
  one atomic directory transaction and contains no `approved` or `promoted` field. L4 alone may
  ask for human approval, promote the exact digest and retain a rollback target.
- Cancellation uses the existing convergent subprocess runner. Repeated cancellation cannot
  return while the direct child comparison process is still alive.

## Fixed v1 acceptance suite

`benchmarks/evolution/python_quality_v1` is the first small host-owned suite. It contains three
deterministic cases: one capability-difference case, one ordinary Python repair that must not
regress, and one deliberately failing verifier that both arms must report honestly. It is a
contract comparison for the Python Quality plugin, not a general coding benchmark and not a model
quality claim.

## Security and trust boundary

The two virtual environments isolate installations, not operating-system authority. Candidate
build/import/runtime code still runs as the current user and only direct child-process convergence
is guaranteed. L3 is suitable for trusted local candidates; untrusted third-party artifacts need
a container or remote sandbox. A deterministic scripted suite also says nothing about real-model
variance, generalisation, token economics or production usefulness.

## Consequences

- The exact L2 artifact becomes a stable comparison input instead of being rebuilt between gates.
- One frozen dependency set and matching install receipts make plugin enablement the only intended
  difference between arms; index timing and independent sdist builds cannot confound the result.
- Runtime, Session, Verifier and request-reconstruction evidence are reused rather than creating a
  benchmark-only execution path.
- An `improved` report is evidence for L4 review, not installation authority.
- L4 must re-read and re-hash the L2/L3 evidence before approval or promotion.

## Rejected alternatives

- **Let the candidate provide tasks, expected answers or evaluator code:** rejected because it can
  redefine success.
- **Compare a rebuilt Wheel:** rejected because it is not the artifact L2 audited.
- **Run baseline without installing the candidate distribution:** rejected because dependency and
  environment differences would be confounded with plugin enablement.
- **Treat a green regression suite as improvement:** rejected because non-regression and added
  capability are different claims.
- **Put comparison in AgentRuntime:** rejected because candidate development is not Turn execution
  or plugin lifecycle ownership.
