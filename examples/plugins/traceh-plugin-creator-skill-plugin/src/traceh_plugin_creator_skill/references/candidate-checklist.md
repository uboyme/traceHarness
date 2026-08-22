# L1 Candidate Static Checklist

Inspect source only. Do not build, import, install, enable or execute it during
L1.

## Workspace boundary

- [ ] The current directory is a dedicated candidate workspace, not the
      TraceHarness core repository or another application.
- [ ] Every created path stays below that workspace.
- [ ] No `.env`, credential, token, user-home configuration, Session data,
      cache, virtual environment, Wheel or build output was created or read.
- [ ] No Git history or remote was changed.

## Identity and packaging

- [ ] Distribution name, import package, Entry Point, entry class, plugin id and
      version match the approved specification.
- [ ] Entry Point group is `traceh.plugins` and its name exactly equals
      `PluginManifest.plugin_id`.
- [ ] Runtime dependency declares `traceharness-py>=0.6,<0.7`.
- [ ] Package discovery uses the `src` layout and all required package resources
      are declared explicitly.

## Architecture and lifecycle

- [ ] Candidate imports author contracts only from `traceh.plugins`.
- [ ] It does not import or modify AgentLoop, AgentRuntime, EventStore, private
      registries or plugin-manager internals.
- [ ] It uses the existing `PluginContext` registration surface and creates no
      parallel loader, runtime, registry, event log or mutable fact source.
- [ ] Every effect has an honest `EffectKind`; cleanup and background tasks use
      activation-owned APIs.
- [ ] Setup contributions close before health; health does not add capabilities.
- [ ] Unsupported v0.6 plugin features are stated as limits, not simulated or claimed.

## Reviewability

- [ ] Tests cover a positive case and a material counterexample; lifecycle or
      effects also have a failure/cancellation/cleanup case.
- [ ] README distinguishes install, discover, doctor, enable and explicit
      Provider/Verifier selection where applicable.
- [ ] `CANDIDATE.md` says **UNVALIDATED (L1 SOURCE ONLY)** and lists capability,
      authority, contributions, files, intended tests, risks and deferred gates.
- [ ] No example or fixture name became an invisible generic default.
- [ ] The final response lists files and risks without pasting full source or
      claiming build/test/safety/performance evidence.

## L2 handoff

- [ ] The operator, not the candidate Agent, will run `traceh plugins validate` from a trusted
      TraceHarness installation.
- [ ] Candidate, trusted core Git repository and new evidence directory are three explicit,
      disjoint paths.
- [ ] Dependency resolution is explicit: `--allow-index` or `--wheelhouse`, never an inferred
      hidden default.
- [ ] A passing L2 report and SHA-256 Wheel are validation evidence only; L3 comparison and L4
      human approval/promotion remain separate.
