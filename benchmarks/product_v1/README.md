# `traceh eval` benchmark: `traceh-product-v1`

Three small, unrelated coding tasks measured through the **same** ProductTask
mainline `traceh chat --product-config` uses: a confirmed proposal, a fixed
Workflow, a managed Git worktree, an immutable Patch Artifact, one frozen
verifier, a Review and a Git ref compare-and-swap promotion.

```powershell
traceh eval benchmarks/product_v1 --output <new-evidence-directory> `
  --provider openai-compatible --base-url <url> --model <model>
```

`--output` must not exist. Every attempt writes its own subtree under
`attempts/<task>/<mode>/<repetition>/`, and the run writes `report.json` and
`report.md` at the root. Nothing is deleted afterwards: an attempt is clean
because its Budget accounts, worktrees and Activations converged, not because its
evidence was removed.

## What the manifest can and cannot say

It names the Profile, the three role slots and their Budgets, the Router bounds,
the aggregate task Budget, the frozen verification plan, the capture limits, the
arms and the tasks.

It **cannot** name a repository, a promotion target, a Workflow node, an edge, an
Agent count, a fan-out or an approval digest. The runner creates a throwaway
source repository and a one-shot local **bare** target for each attempt, which is
why this command structurally cannot touch a real remote.

It also cannot name a provider or a model. Those come from `--provider` /
`--model` (or `TRACEH_PROVIDER` / `TRACEH_MODEL`), so one run uses one model
family for every arm and the report records which one.

## Reading the report

* Quality aggregates are keyed by **resolved** mode. An `auto` attempt whose
  Router chose `multi` is counted in the `multi` arm; `auto` appears separately
  only as routing cost and routing outcome. It is not a third quality arm.
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
