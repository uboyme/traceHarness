# ADR-0018: Human-approved exact plugin promotion and rollback

## Context

ADR-0016 binds one independently validated plugin Wheel to L2 evidence. ADR-0017 compares that
same Wheel with a disabled baseline under a fixed host-owned suite, but deliberately grants no
installation authority. A comparison label is not consent, and installing a rebuilt or changed
artifact would break the evidence chain. Promotion also changes an external Python environment,
so cancellation, concurrent operators and crashes need an explicit owner and recovery state.

## Decision

- L4 remains under `traceh.evolution`; it does not add approval, pip or rollback work to
  `AgentRuntime`, `AgentLoop`, `PluginManager`, Session state or the Event Log.
- `traceh plugins promote` is a two-invocation protocol. Without `--approve`, it re-reads the L2
  and L3 reports, audits the exact Wheel, inspects an explicitly selected Python interpreter and
  writes a Chinese evidence/risk card plus an approval SHA-256. It does not create the Registry
  or change the target environment. A second invocation must supply that complete digest.
- Only a successful, internally consistent L3 `improved` result with at least one improvement and
  zero regressions is promotable. L4 reconstructs every arm result, Case outcome, summary,
  classification, canonical L3 gate and non-empty frozen Wheel set; a report-shaped skeleton is
  not evidence. Human approval cannot override a known regression.
- The approval digest binds the complete L2/L3 report bytes, exact Wheel digest and candidate
  identity, selected Registry, target interpreter path and Python identity, target Distribution
  receipt, installed-package content digest, canonical package owner, current managed promotion
  and observed improvement/regression lists. Any change makes the prior approval stale.
- The target must expose the same `traceharness-py` version and the same non-candidate Distribution
  name/version receipt as the compared L3 environment. L4 v1 installs only the exact candidate
  Wheel with `--no-index --no-deps`; it will not silently resolve a new dependency set during
  promotion. After installation the complete receipt must equal L3, `plugins doctor` must pass,
  and a second complete receipt must still equal the pre-doctor observation and L3. A bounded
  digest over every non-cache file below the target's `purelib`/`platlib` roots is also captured
  immediately before and after doctor, so same-version changes to another Distribution and files
  absent from the candidate's `RECORD` are detected. Regenerable `__pycache__` content is excluded
  so ordinary target execution does not create false drift. Import/health side effects therefore
  cannot be recorded as a false stable promotion.
- Target inspection runs with `-I -S` so candidate `.pth` and startup hooks do not execute. Because
  `-S` also suppresses virtual-environment prefix initialisation, the probe preserves the selected
  executable path, locates its adjacent `pyvenv.cfg`, and supplies that venv root as `base` and
  `platbase` to `sysconfig`; it must not fall through to the host/base interpreter's packages.
- The first promotion refuses to take over an already installed, unmanaged candidate
  Distribution. Later operations require the installed candidate content receipt and complete
  environment receipt to match the Registry's stable state.
- A fixed host-owned coordination namespace beside the canonical target environment keys one
  owner record and cross-process lock by the canonical target environment alone. It does not
  depend on temporary-directory settings, interpreter aliases, caller-selected Registry,
  plugin id or Distribution. Because each Distribution state records the complete environment,
  L4 v1 permits only one active managed Distribution chain per target environment; another
  Distribution is rejected until the current chain is fully rolled back to absent and releases
  the owner. This prevents concurrent L4 mutations and contradictory full-environment facts.
  The selected Registry still owns immutable Wheel copies, promotion records, receipts and a
  `stable / installing / rollbacking` state machine keyed by target plus Distribution; only the
  chain named by the environment owner is authoritative. State and immutable evidence are
  fsync-written then atomically replaced before a process may report a stable transition.
- Before pip runs, the exact artifact and promotion record exist and state becomes `installing`.
  If the process is killed after the first owner/record write but before that first state write,
  explicit rollback reconstructs only that safe pre-pip state from the exact record and an absent
  target; any contradictory evidence fails closed.
  Success records the installed content receipt and returns to `stable`. Ordinary failure or
  cancellation restores the previous exact Wheel, or uninstalls the first promotion, before the
  caller returns. The restored candidate and complete environment receipt must equal the previous
  stable state before that state is written again. Repeated cancellation cannot abandon that
  convergence.
- `traceh plugins rollback` requires the canonical Distribution and exact current promotion id. It can also recover an
  `installing` or `rollbacking` crash state when the supplied id names the unfinished source.
  Rollback installs the Registry's previous SHA-256-addressed Wheel or performs the recorded
  first-install uninstall; an unknown, stale or drifted state fails closed.
- The Registry is the promotion-state authority. JSON/Markdown command output is an atomic mirror.
  If promotion-report publication fails, the just-installed candidate is rolled back. A report
  failure after a successful rollback cannot undo the already recorded recovery.
- Review output and Registry paths must be outside the inspected target prefix. Review therefore
  cannot mutate the target merely by choosing an output path, and Registry bookkeeping cannot hide
  inside an installed-package receipt.

## Security and trust boundary

L4 is a same-user local control plane, not an operating-system sandbox or a package signature
system. It prevents accidental artifact substitution, stale approval, concurrent L4 mutation and
unmanaged takeover, but another process with the same filesystem and environment authority can
still alter files or run pip outside the Registry lock. Target metadata inspection uses isolated
Python startup without candidate import; the explicit doctor step imports the approved plugin.
L4 v1 does not install new dependencies, migrate Sessions, enable a plugin in a running Runtime or
prove that a deterministic suite generalises to real models. It also does not coordinate multiple
managed Distributions inside one target environment; that requires a future unified environment
transaction and state model rather than independent package records.

## Consequences

- A human reviews concrete evidence and approves one immutable transaction, not a plugin name.
- L2 build identity, L3 observed improvement and L4 installation authority remain separate.
- A crash cannot turn an unfinished pip operation into a false stable state; the prior artifact is
  retained as a deterministic repair target.
- Operators need a dedicated target environment whose dependency receipt matches L3. More general
  dependency upgrades require a future separately approved environment-transaction design.

## Rejected alternatives

- **One `--yes` flag:** rejected because it is reusable and does not bind evidence or target state.
- **Install directly from the candidate source or rebuild at L4:** rejected because those bytes
  were not the artifact L2 audited and L3 compared.
- **Let improved automatically install:** rejected because evaluation is evidence, not authority.
- **Resolve dependencies during promotion:** rejected because the installed environment would no
  longer be the compared environment.
- **Put promotion in AgentRuntime:** rejected because package management is not Turn execution or
  Generation ownership.
