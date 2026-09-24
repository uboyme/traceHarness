# Task-type benchmark (Single/Multi applicability)

Five self-built Product tasks on **one pinned code base**: the verified upstream
tree of SWE-bench Lite `pylint-dev__astroid-1196` (272 files, LGPL-2.1-or-later,
license retained under `../real_repository_v1/licenses/`). Holding the code base
constant leaves task structure as the only intended difference between cases.
This is a small case study for plan S0-E / experiment A of the
[collection plan](../../docs/plan/TRACEHARNESS_TASK_TYPE_CONTEXT_EVOLUTION_PLAN.md),
not a leaderboard, not an unseen holdout and not evidence that either mode is
generally better.

| Case | Category | Agent-visible task | Acceptance | Pre-registered expectation |
|---|---|---|---|---|
| `a1-small-hashlib-blake2` | small | one injected defect in one module | hidden tests + upstream `unittest_brain.py` | Single cheaper at equal quality |
| `a2-parallel-read-brain-registry` | parallel read | structured facts about 8 independent modules in `FINDINGS.json` | host answer computed from the syntax tree; `astroid/` and `tests/` must be unchanged | Multi possibly faster at similar tokens |
| `a3-parallel-fix-three-brains` | parallel write | three independent injected defects in three modules | hidden tests + upstream `unittest_brain.py` | Multi possibly faster, more tokens |
| `a4-investigate-negative-subscript` | investigate then fix | symptom only; root cause in core inference | hidden tests + upstream `unittest_inference.py` | uncertain |
| `a5-dependent-sample-arguments` | strong dependency | three ordered changes to one function | hidden tests + upstream `unittest_brain.py` | Single better |

The case definitions live in
[`tests/task_type_evaluation/spec.py`](../../tests/task_type_evaluation/spec.py)
as exact single-occurrence text edits (`inject` for the Agent's tree, `reference`
for the tree that must pass). Hidden tests are under `hidden/<case>/`; the a2
checker is a template whose expected answer the builder computes from the
pristine source, never hand-written. Nothing under `hidden/` or any reference
tree enters an initial tree.

`calibration.json` freezes, per case, the fail-to-pass tests (failing on the
initial tree, passing on the reference) and pass-to-pass tests (passing on both),
measured through the production sandbox and bound to `spec_digest`. Changing a
case, a hidden test or the oracle changes that digest; the builder then refuses
the old calibration and the case must be recalibrated and re-admitted as a new
version. Results obtained on one version are never re-scored on another.

## Reproduce the offline steps

These commands are examples; choose new output directories and an explicit host
sandbox policy. They load no model, `.env` or API key.

```powershell
$env:PYTHONPATH = 'tests'; $env:PYTHONUTF8 = '1'
python -m task_type_evaluation.build --cache .traceh/rr-eval/cache --output <prep-dir>
python -m task_type_evaluation.calibrate calibrate --prep <prep-dir> `
  --sandbox <sandbox.json> --output <cal-dir> --destination benchmarks/task_type_v1/calibration.json
python -m task_type_evaluation.build --cache .traceh/rr-eval/cache --output <built-dir> `
  --calibration benchmarks/task_type_v1/calibration.json --template <benchmark-template.json>
python -m task_type_evaluation.calibrate admit --built <built-dir> `
  --sandbox <sandbox.json> --output <admission-dir>
```

The cached archive is the one bound by `../real_repository_v1/selection.json`;
the builder re-checks its digest and every upstream blob before use.
