---
name: traceh-plugin-creator
description: Create a source-only TraceHarness Python Entry Point plugin candidate in a dedicated workspace when the user explicitly asks to add a new TraceHarness plugin capability.
---

# TraceHarness Plugin Creator

Create a reviewable plugin candidate without changing the TraceHarness core or
installing unreviewed code. This is L1 of the controlled capability-evolution
flow: it produces source files only. Build, test, comparison, approval,
installation and rollback belong to later gates.

## Mandatory safety gate

Before writing anything:

1. Confirm that the current workspace is a dedicated candidate directory. It
   may be empty or contain only this candidate. If it is the TraceHarness core
   repository (for example its project is named `traceharness-py` or it contains
   `src/traceh`), stop and ask for a separate workspace.
2. Do not navigate outside the current workspace or write to another project.
3. Do not read or create `.env`, credentials, tokens, user-home configuration,
   TraceHarness Session data or network resources.
4. Do not run the candidate, import it, build a Wheel, invoke its tests, install
   it, enable it, commit it or push it. Later stages own those actions.

These rules reduce accidental damage; they are not a sandbox. The operator must
still run TraceHarness with a genuinely separate Candidate Workspace.

## Required explicit specification

Collect these facts before creating files. If a value is missing, propose a
value and wait for explicit confirmation instead of silently choosing one:

- capability purpose and observable acceptance criteria;
- plugin id, distribution name, import package, entry class and version;
- requested contribution kinds and their public names;
- TraceHarness compatibility range;
- any external dependency, filesystem, process or network authority;
- expected failure behaviour, cleanup needs and known risks.

Do not copy identifiers or behaviour from an example plugin as a hidden
default. Examples are evidence of the contract, not product requirements.

## Authoring workflow

1. Call `traceh_plugin_creator_guide` with `topic="contract"`.
2. Call it with `topic="template"` and select only the patterns needed by the
   approved specification.
3. Create the complete independent distribution in the current workspace.
4. Call it with `topic="checklist"` and inspect every generated file against
   that checklist. Static inspection is allowed; execution is not.
5. Write `CANDIDATE.md` as a plain-language, non-authoritative review card. Mark
   the candidate **UNVALIDATED (L1 SOURCE ONLY)** and list capability, authority,
   contributions, files, intended tests, risks and deferred validation.
6. Return a concise file-and-risk summary. Write source to files; do not paste
   the full candidate implementation into the conversation.

## Completion boundary

L1 is complete only when the candidate contains its package metadata, Entry
Point, `PluginManifest`, implementation, tests, README and `CANDIDATE.md`, and
all identities agree by static inspection. Never claim that it builds, imports,
passes tests, is safe, is installed or improves the Agent. Those claims require
evidence from later gates.
