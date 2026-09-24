# `traceh eval` benchmark: `traceh-product-v1`

Three small, unrelated coding tasks measured through the **same** ProductTask
mainline `traceh chat --product-config` uses: a confirmed proposal, a fixed
Workflow, a managed Git worktree, an immutable Patch Artifact, a case-owned frozen
verifier, a Review and a Git ref compare-and-swap promotion.

```powershell
traceh eval benchmarks/product_v1 --output <new-evidence-directory> `
  --provider openai-compatible --base-url <url> --model <model> `
  --sandbox-config <host-policy-file>
```

`--output` must not exist. Every attempt writes its own subtree under
numbered `attempts/001/` directories (mapped to task/mode/repetition in the report), and writes `report.json` and
`report.md` at the root. Nothing is deleted afterwards: an attempt is clean
because its Budget accounts, worktrees and Activations converged, not because its
evidence was removed.

The frozen verifier uses protocol 3 and the explicitly selected host sandbox.
Its guest environment does not inherit the host PATH or temporary directories.
Sandbox receipts and output digests use the attempt's original EventStore and CAS;
missing sandbox configuration refuses process execution. No image is pulled automatically.

## What the manifest can and cannot say

It names the Profile, coder and readonly investigator templates and their Budgets,
the aggregate task Budget, capture limits, single/multi modes and a separately
hashed format-3 Product dataset. Each case freezes its verification plan and explicit
`initial_tree_limits` (`max_files`, `max_file_bytes`, `max_total_bytes`). Format 2
is rejected without automatic migration. Repetitions
belong to RunOptions / run plan. Removed auto/multi and Router fields are rejected.

It **cannot** name a repository, a promotion target, a Workflow node, an edge, an
Agent count, a fan-out or an approval digest. The runner creates a throwaway
source repository and a one-shot local **bare** target for each attempt, which is
why this command structurally cannot touch a real remote.

It also cannot name a provider or a model. Those come from `--provider` /
`--model` (or `TRACEH_PROVIDER` / `TRACEH_MODEL`), so one run uses one model
family for every arm and the report records which one.

## Reading the report

* Quality aggregates follow the explicit `single` or `multi` mode. Execution
  cost includes the complete owned Agent tree, including failed/cancelled children.
* An arm with one observation is labelled `single observation`. Aggregates are
  counts, totals, minima, maxima and a mean - no significance is claimed.
* `approval_wait_ms` is measured separately and excluded from `active_ms`. This
  benchmark approves programmatically and immediately (`approval_policy:
  programmatic-immediate`), which is stated in both outputs.
* A metric the durable facts could not support is reported as *unavailable*,
  never as zero.

## Known limits

* The frozen verifier proves the declared checks passed on the reviewed bytes.
  It does **not** prove a candidate left those checks as strong as it found them;
  a candidate that weakens a test still has to pass the human/host approval gate,
  but the verifier alone will not catch it.
* `argv` names `python`. That is an explicit host decision in the manifest; edit
  it if the interpreter you want is called something else on your `PATH`.
* Three tasks with a handful of repetitions is a sanity measurement, not a
  ranking.

当前根 benchmark protocol 为 3，task_settings 必须有 retrieval；本编码基线设为 null。旧根 1/2 拒绝，内层 Verifier protocol 为 2。检索基线见 [retrieval_v1](../retrieval_v1/README.md)。

UE-1 共用 EvaluationRunner；公共 trials 是完整运行分母，原 Product 指标位于 task_report。
配置示例见 [run-plan](run-plan.example.json)，字段和运行说明见 [UE 合同](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE0_CONTRACT.md)。
