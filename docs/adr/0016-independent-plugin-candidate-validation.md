# ADR-0016: Independent plugin candidate validation

## Context

ADR-0015 deliberately stops at source-only candidate authoring. A candidate and its authoring
Agent are not trustworthy sources for claims such as "the Wheel builds", "the tests pass" or
"the core still works". Running those checks inside `AgentRuntime`, `AgentLoop` or the plugin
activation transaction would also mix a development workflow with the execution plane and make
the runtime own build tools, virtual environments and arbitrary candidate processes.

## Decision

- L2 is a development control plane in `traceh.evolution`, exposed by
  `traceh plugins validate`. It does not add an Event, Session, Generation, Registry or plugin
  loader path, and it does not modify `AgentLoop` or `AgentRuntime`.
- The caller must provide three disjoint paths: a source-only candidate, a trusted TraceHarness
  Git repository, and a new output directory. Dependency resolution is also explicit: either
  permit the configured package index or provide a wheelhouse. Missing or ambiguous values fail
  closed; the validator does not infer a candidate identity or dependency source. Candidate
  build/runtime and explicit test requirements cannot use direct URL/file references that bypass
  this boundary.
- The candidate is copied without symbolic links, Windows Junctions or other reparse points,
  `.env`, Wheels, caches, VCS state or build products and under fixed file/byte budgets. The
  trusted regression input is a detached clone of the explicitly supplied repository's `HEAD`;
  uncommitted core files cannot weaken the evaluator. Compatibility is checked against the one
  literal `__version__` statically read from that clone, not the running CLI's version.
- Core and candidate Wheels are built separately. The candidate Wheel is audited for path
  traversal, encrypted members, bytecode/caches, `.pth`, Python startup hooks, symbolic-link
  members, extra top-level packages, and collisions with standard-library or host-control import
  roots such as `traceh` and `pytest`.
- Candidate contract/doctor/tests and trusted core regression run in separate temporary virtual
  environments. Candidate pytest configuration cannot replace the host-owned pytest config;
  plugin autoload and ambient `PYTHONPATH` are disabled, and installed metadata is checked through public
  `PluginDiscovery`. Both environments install the same audited bytes before candidate code is
  executed. The core regression installs the candidate but never enables it.
- Candidate-controlled stdout/stderr is not copied into reports. Reports contain bounded,
  host-authored Chinese summaries and stable codes. A failed trusted core regression may write
  a bounded tail of trusted pytest output to a separate diagnostic file.
- The initial Wheel audit anchors its budgeted bytes and SHA-256 in host-process memory. After
  candidate execution, publication re-audits the build output and installation snapshot, checks
  both against that digest, and creates the artifact only from the anchored bytes.
- The Wheel, JSON report, Markdown report and optional diagnostic are written to a same-filesystem
  sibling staging directory. One directory rename exposes the complete bundle. Ordinary gate
  failure publishes a complete report-only bundle; report or commit failure leaves the requested
  output path absent, never a lone Wheel or half-report.
- Direct child processes converge before cancellation returns. Repeated cancellation cannot
  release the caller while that child is still alive.

## Gates

The ordered transaction is: source contract; trusted `HEAD` snapshot; core and candidate Wheel
builds; candidate Wheel audit; candidate environment install; installed metadata contract;
plugin doctor; candidate test collection and execution; regression environment install; full
trusted core regression; validated artifact publication.

## Security and trust boundary

Temporary directories and virtual environments isolate files and Python installations; they are
not an operating-system sandbox. Candidate build, import, doctor and tests still execute with the
current user's permissions, and only direct child-process convergence is guaranteed. L2 therefore
requires a trusted operator and should move to a container or remote sandbox before evaluating
untrusted third-party source. An index-enabled run may access the network; `--wheelhouse` is the
explicit offline alternative. A same-user process can also modify ordinary output files after the
command returns, so L4 must re-check the report SHA-256 before promotion.

## Consequences

- L2 proves a reproducible build and the listed gates against one explicit core commit. It does
  not prove that the capability is useful, safer or better than a baseline.
- L3 owns baseline/candidate comparison. L4 owns human approval, promotion and rollback. L5 may
  propose candidates but cannot approve or promote them.
- The exact validated Wheel and report can become L3 input without rebuilding the candidate.
- The control plane intentionally reuses the public plugin SDK, doctor command and existing test
  suite while keeping the execution-plane architecture unchanged.

## Rejected alternatives

- **Let the candidate write or select its evaluator:** rejected because it could weaken tests or
  redefine success.
- **Validate against the dirty core working tree:** rejected because uncommitted evaluator changes
  are not a stable trust anchor.
- **Publish or report directly into the requested output directory:** rejected because later test,
  report or filesystem failure would leave a misleading Wheel or a half-written evidence set.
- **Call the venv a sandbox:** rejected because it does not constrain filesystem, process or
  network authority.
