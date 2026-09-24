# Offline real-repository acceptance evidence

`verified-summary.json` records the final six material-admission observations,
three reference-driven Product attempts, three intentionally unfixed attempts,
closed Budget/Workspace outcomes, report hashes and the independent reread result.
`environment.json` records the inspected dependency image and installed packages.
`regression-summary.json` binds four scoped JUnit batches, retaining earlier failures
and their subsequent owner reruns. The latest outcomes of 269 distinct nodes pass;
this is not one full-suite run and does not include unrun release gates.

These are derived, reviewable summaries, not replacement event stores. Original
SQLite streams, CAS, frozen material/source archives, Git targets and raw admission
output remain under `.traceh/rr-eval/` in this workspace. The relative source paths
and hashes identify those retained artifacts; this small directory alone is not
a portable copy of all execution evidence.

The production evidence loader revalidated both runs. Existing Session invariants,
request reconstruction and Product metric readers rechecked 24 request snapshots
and six attempts, including each actual target ref. All database file hashes were
unchanged by rereading. No external model was called; reference injection must not
be reported as agent success, statistical significance or a cost improvement.

Selection and reproduction instructions: [materials](../../../benchmarks/real_repository_v1/README.md).
Scope, failures and gates: [record 077](../../deal/077-real-repository-evaluation.md).
