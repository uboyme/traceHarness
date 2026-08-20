# ADR-0015: Plugin candidate authoring is a source-only external skill

Status: Accepted

## Context

TraceHarness v0.5 can load, replace and retire independently distributed plugins without
changing `AgentLoop`. The next product goal is controlled capability evolution: an Agent may
propose and implement a new capability, but candidate code must not enter the trusted runtime
before later build, test, comparison and human-approval gates exist.

Putting candidate generation into `AgentRuntime`, `AgentLoop` or `PluginManager` would mix a
development control plane with execution lifecycle. Adding a new writer or installer would
also duplicate the existing workspace Tool and plugin activation mainlines before there is an
approval protocol.

## Decision

- L1 is an independent `traceh.plugins` distribution named
  `traceh-plugin-creator-skill-plugin`, enabled explicitly as `traceh.plugin.creator`.
- It contributes one short Prompt section and one `PURE_READ` guide Tool. Detailed workflow,
  SDK contract, package template and static checklist are packaged Markdown resources read
  through `importlib.resources`.
- Candidate source is written only by the existing workspace-confined coding Tools. L1 adds no
  writer, installer, registry, Generation manager, EventStore, mutable message state or event
  type. `AgentLoop`, `AgentRuntime` and `PluginManager` are unchanged.
- The operator supplies a dedicated Candidate Workspace outside the TraceHarness core. The
  skill must stop if it detects the core repository, must collect or confirm all candidate
  identities and authority before writing, and must not read secrets or user-home config.
- L1 creates source only: package metadata, Entry Point, `PluginManifest`, implementation,
  tests, README and a plain-language `CANDIDATE.md` marked
  `UNVALIDATED (L1 SOURCE ONLY)`. It must not build, import, execute, test, install, enable,
  commit or push the candidate.
- The skill's restrictions are workflow rules, not a sandbox. Later gates must independently
  build and execute untrusted candidates in an appropriate isolated environment and must not
  trust a candidate's own report.
- Example plugins are contract evidence only. Their identifiers and behaviour are never
  candidate defaults; missing or ambiguous inputs require explicit confirmation.

## Mainline acceptance

The clean-venv Wheel E2E copies only each project's declared build inputs, builds and audits
the core plus the Skill example, Python Quality and Plugin Creator distributions, then installs
them offline. A Scripted Provider calls the creator's guide through the normal ToolRuntime,
Event/Effect ledger and Composition Snapshot path, while the dedicated workspace remains
unchanged. Discovery, `inspect` and `doctor` use the existing plugin CLI. The archive audit
rejects bytecode, caches, old build trees and egg metadata before installation.

## Consequences

- The current TraceHarness Agent can consume the authoring skill without core changes.
- The same packaged instructions can guide another coding agent, but they remain a project
  artifact rather than a hidden Codex/Claude user-directory dependency.
- L1 can generate reviewable source, but cannot honestly claim build success, test success,
  safety, quality improvement or installation.
- L2–L5 can add validation, comparison, approval/promotion and weakness proposals as separate
  control-plane services. They must reuse existing Verifier, Evaluation, plugin activation and
  Generation contracts instead of moving those workflows into `AgentRuntime`.

## Rejected alternatives

- **Add `traceh plugins create` to the core CLI now:** rejected because source authoring does
  not require a new Runtime product state or command before the workflow contract stabilises.
- **Add a workspace-writing scaffold Tool to the skill:** rejected for L1 because the existing
  coding Tools already provide the evidenced write path; another writer would duplicate
  confinement and effect semantics.
- **Let the Agent run pip, tests or doctor immediately:** rejected because candidate execution
  before an independent validation gate collapses proposal and approval into one authority.
- **Read Codex/Claude skill folders from the user's home directory:** rejected because the
  project artifact must be versioned, packaged and reproducible without private local state.

## Follow-up

L2 is now implemented as the independent validation control plane recorded in
[ADR-0016](0016-independent-plugin-candidate-validation.md). This does not change L1's
source-only authority or retroactively make its candidate card validation evidence.
