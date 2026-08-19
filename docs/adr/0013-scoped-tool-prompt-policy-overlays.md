# ADR-0013: Scoped Tool, Prompt and Policy overlays use the Generation mainline

Status: Accepted

## Context

ADR-0012 established deterministic Application → Workspace → Preset → Agent resolution for
Services. Tool, Prompt and Policy are different because they affect execution and the model
request: their effective values must be frozen by a Composition Generation and represented in
the Composition Snapshot. Keeping four mutable registries alive beside the Generation would
create a second fact source and could let one Step observe more than one composition.

Application plugins also contribute Tool and Prompt values after setup. A child overlay that
is valid against the core alone may become an implicit shadow after a plugin adds an ancestor
with the same name. Detecting that only after health checks would execute third-party code for
a candidate already known to be invalid.

## Decision

- Host assembly accepts immutable `ScopedToolBinding`, `ScopedPromptBinding` and
  `ScopedPolicyBinding` values. Plugin setup remains application-only.
- `CompositionOverlayPlan` sorts bindings by the fixed scope order and resolves them on
  private Tool and Prompt forks. It produces one effective `ToolRegistry`,
  `PromptAssembler` and Policy tuple; it does not remain as a mutable runtime hierarchy.
- Tool name, Prompt section id and Policy name are the scoped identities. A same-scope
  duplicate requires `replace=True` and otherwise reports `*-already-bound`. A child override
  requires `replace=True` and otherwise reports `*-override-requires-replace`.
- `replace` must be a real boolean. Truthy strings, integers and other values are rejected.
- Application-only bindings are resolved before plugin setup. Child bindings are first
  preflighted against that candidate, then revalidated against a private projection of staged
  plugin Tool/Prompt contributions before health checks, and finally resolved after real
  plugin publication.
- The effective Tool/Prompt/Policy values transfer together in `PluginActivationSet`.
  `CompositionGeneration` requires its `ToolRuntime` to use that set's Tool Registry and
  the same ordered Policy objects, compared by identity rather than caller-controlled value
  equality, then freezes the existing Snapshot inputs. Plugin replacement retains the same
  child overlay blueprint.
- `PromptAssembler.register(..., replace=True)` returns a reversible registration that
  restores the previous section during normal reverse-order cleanup.

## Ownership and persistence

Host-provided scoped Tool and Policy objects are borrowed; their assembler retains lifecycle
ownership. Application plugin Tool/Prompt resources remain owned by their ActivationSet.
The overlay hierarchy has no persistent identity and adds no event. Its model-visible result
is already covered by Tool schemas, assembled Prompt and Policy names in the existing
Composition Snapshot and request fingerprint.

## Consequences

- Two independently assembled Runtime/Agent scopes can have different effective Tool,
  Prompt and Policy compositions without sharing mutable registries.
- An in-progress Step remains bound to its Generation; a later plugin replacement cannot
  modify that Step in place.
- A plugin application contribution that creates an unapproved child shadow fails before
  health code runs and retains a stable conflict code and responsible plugin id.
- `AgentLoop`, Event Log, request reconstruction and replay require no new branch.
- This does not let plugins select Workspace/Preset/Agent setup, provide Policy through
  `PluginContext`, or create/supervise multiple Agents.

## Rejected alternatives

- **Four live ToolRuntime/Prompt/Policy registries:** rejected because lookup during a Step
  would create a mutable fact source outside the Generation Lease.
- **Resolve child overlays only before plugin setup:** rejected because late application
  contributions could bypass explicit replacement intent.
- **Resolve conflicts after health checks:** rejected because a deterministically invalid
  candidate must not receive another third-party execution opportunity.
- **Persist scope identity:** rejected because request reconstruction needs the effective
  model-visible composition, which the existing Snapshot already records, not assembly
  provenance.
- **Widen `PluginContext` in the same change:** rejected because plugin Policy and other
  contribution categories require separate trust, ownership and failure decisions.
