# Real repository development materials

Three public SWE-bench Lite **dev** instances, retaining their complete tracked
ordinary-file trees. This is a reproducible integration set, not a leaderboard,
an unseen holdout, or evidence that multi-agent execution improves results.

| Instance | Repository bytes / files | Defect | License retained |
|---|---:|---|---|
| marshmallow-code__marshmallow-1359 | 720107 / 78 | Nested DateTime fields fail to inherit schema formatting | MIT |
| pydicom__pydicom-1694 | 9380936 / 478 | Invalid raw data elements bypass JSON exception suppression | MIT |
| pylint-dev__astroid-1196 | 2030097 / 272 | Dictionary unpacking does not infer referenced values | LGPL-2.1-or-later |

Sources: [SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2),
[marshmallow PR](https://github.com/marshmallow-code/marshmallow/pull/1359),
[pydicom PR](https://github.com/pydicom/pydicom/pull/1694),
[astroid PR](https://github.com/pylint-dev/astroid/pull/1196).
Instance numbers identify the upstream changes collected by SWE-bench; they are
not necessarily the issue numbers mentioned in the problem statement.

`selection.json` freezes dataset revision, source archives and hashes, complete
upstream Git inventories, quotas, import roots, protected support files, case-explicit
writable ordinary-test patterns and exact test ID
maps. `instances/` retains the original problem, reference patch and test patch;
`trees/` binds each original path, mode and Git blob SHA; `licenses/` preserves the
upstream license texts. Preparation verifies every byte and refuses omitted files,
extra files, links, oversized material, and identity/hash mismatches. The local
deterministic source commit is distinct from the upstream commit. POSIX executable
bits are recorded in provenance but are not reproduced by the current Windows
Product initial-tree protocol.

Selection happened before any model execution. SQLFluff 1625 was excluded because
its oracle only checked a message change; 1733 contained a symlink unsupported by
the existing workspace contract. Neither was silently reduced to a smaller tree.
The eight explicit marshmallow test-ID mappings resolve truncated historical
log identifiers to full collected pytest IDs; runtime matching remains exact.

## Reproduce the offline checks

These commands are examples. Select new output directories and an explicit host
sandbox policy; the helpers never load a real model, `.env`, or an API key.
The host needs TraceHarness development dependencies and Git. Dependency building
uses the network; subsequent sandbox workloads use `network=none`.

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'tests'
docker build --platform linux/amd64 -t traceh-rr-eval:local benchmarks/real_repository_v1
docker image inspect traceh-rr-eval:local --format '{{.Id}}'
python -m real_repository_evaluation.materials `
  --selection benchmarks/real_repository_v1/selection.json `
  --cache .traceh/rr-cache --output .traceh/rr-materials `
  --template benchmarks/product_v1/benchmark.json
python -m real_repository_evaluation.preflight `
  --materials .traceh/rr-materials --sandbox <explicit-sandbox.json> `
  --output .traceh/rr-admission
python -m real_repository_evaluation.acceptance `
  --materials .traceh/rr-materials --sandbox <explicit-sandbox.json> `
  --output .traceh/rr-reference --variant reference
python -m real_repository_evaluation.acceptance `
  --materials .traceh/rr-materials --sandbox <explicit-sandbox.json> `
  --output .traceh/rr-negative --variant unfixed
python -m real_repository_evaluation.recheck `
  --run .traceh/rr-reference --materials .traceh/rr-materials `
  --output .traceh/rr-reference-recheck.json
```

The tested host policy uses format 2, no plugin grants, the inspected image digest,
`desktop-linux`, read/write scope `.` and no exclusions. Explicit resource bounds:
512 MiB memory, 128 MiB workspace, 8192 workspace entries, 1 MiB output, 64 PIDs,
2 CPUs and 180 seconds per command. These are material-host settings, not runtime
defaults. The dependency recipe pins Python and packages; the resulting image ID
and installed package list must be recorded for each run. Rebuilding does not
promise a byte-identical image or a model revision.

Generated `material/` uses the current format-3 Product dataset. Reference answers
and test preparation stay in sibling `host/`; neither enters an agent initial tree.
Only the frozen verifier command contains the hidden-test payload. Material plan
version 3 excludes each case's declared ordinary regression-test paths from the
support-file hash check and still checks protected fixtures/configuration. It
fully replaces the selected test files with host-frozen versions,
then installs the upstream test patch into the disposable
sandbox copy. The image contains dependencies only. Missing, skipped and setup-error
tests cannot substitute for required passes. This protects test-material integrity;
it is not a claim that arbitrary malicious Python cannot interfere with pytest.

Admission runs the actual sandbox and records full node IDs and receipts. It must
show each FAIL_TO_PASS test failing in its call phase before repair, all required
PASS_TO_PASS tests passing, and the entire selected test-file suite passing after
the reference repair. Raw admission output is retained outside agent workspaces.
Product verification keeps its existing output-disclosure policy.

`acceptance` is an explicitly named **reference answer injector**, using the original
EvaluationRunner → ProductTask → Workspace → Artifact → Verifier → Approval → Git
promotion path. Its zero model-token usage is a property of an offline mechanism
test. It is never an autonomous agent score, a model-cost measurement, or an
official SWE-bench harness result. `unfixed` makes a real source change while
retaining the defect, so rejection cannot be credited to an empty patch.

Current measurements and unfinished gates live in [record 077](../../docs/deal/077-real-repository-evaluation.md).
Live single/multi and bounded-optimization experiments need a separately frozen
protocol, independent held-out tasks, repetitions, model identity and spend limit.

## 扩集候选（尚未准入）

`expansion-candidates.json` 固定同一数据集 revision 的 23 个 dev / 300 个 test 元数据候选及顺序。
目标开发 10、留出 30，规则与校准/统计边界见[执行计划](../../docs/plan/TRACEHARNESS_REAL_REPOSITORY_EVALUATION_PLAN.md#rr-6扩集与统计预登记本轮冻结不提前宣称准入完成)。
候选不包含新增可运行题目承诺，不计入当前三题的准入数量；不得依据模型结果替换题目。

The live pilot exposed two failures of earlier material versions: version 1 rejected
candidate edits to selected tests before replacing them, and version 2 still rejected
ordinary edits to unselected test modules. Version 3 uses the explicit patterns above.
Independent replays of the two originally rejected model patches pass their frozen
tests under version 3; the original Product outcomes remain failed. Test-evasion
and support-file tampering controls are retained. See [record 078](../../docs/deal/078-real-model-repository-pilot.md).
