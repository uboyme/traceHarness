# TraceHarness Py v0.3 release validation

Validated on 2026-08-16 with CPython 3.13.5. The project declares support for
Python 3.12 and newer; the included CI matrix covers Python 3.12 and 3.13.

## Checks performed

| Check | Result |
|---|---|
| `python -m compileall -q src tests` | Passed |
| `PYTHONPATH=src pytest -q` | 24 passed |
| `coverage run --source=src/traceh -m pytest -q` | 80% total statement coverage |
| Deterministic coding demo | Passed in 4 Steps |
| Demo durable Session events | 48 |
| Demo invariant violations | 0 |
| Demo request-reconstruction violations | 0 |
| Demo external command verification | Passed |
| Included benchmark | 1/1 cases passed (100%) |
| Wheel metadata inspection | Passed |
| Clean virtual-environment wheel install | Passed |
| Installed `traceh doctor` and package import | Passed |

## Deterministic demo behavior

The bundled Scripted provider performed the following real operations against a copied
workspace:

1. Read `calculator.py`.
2. Replaced `return a - b` with `return a + b` through `apply_patch`.
3. Ran `python -m unittest -v` through the unified Shell tool.
4. Finished only after the external verifier passed.

The generated Session could be inspected and replayed from JSONL, and all four persisted
model requests were independently reconstructed without a fingerprint mismatch.

## Scope of the release

v0.3 is a single-Agent, local-runtime base. It includes the stable seams needed for later
plugin and multi-Agent work—Protocols, Scope, reversible Activation, typed Hooks,
Composition leases, Effect records, AgentSupervisor DTOs and WorkspaceProvider DTOs—but
it intentionally does not claim that the future PluginManager, live AgentSupervisor,
workflow engine or remote sandbox is already implemented. See `ROADMAP.md` and
`docs/plugin-evolution.md`.
