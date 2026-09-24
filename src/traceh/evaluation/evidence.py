"""Shared offline validation of closed evaluation runs; never execute a trial."""

import zipfile

from traceh.api.json_types import fingerprint, to_json_value
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import artifact_digest, read_input
from traceh.evaluation.variants import source_digest


def load_run(root):
    from traceh.evaluation.runner import _evidence_files

    frozen_file, report_file, evidence_file = (
        read_input(root, name) for name in ("frozen.json", "report.json", "evidence-manifest.json")
    )
    frozen, report, evidence = frozen_file.data, report_file.data, evidence_file.data

    def require(condition):
        if not condition:
            raise BenchmarkManifestError("evaluation-evidence-mismatch", "run")

    require(
        frozen["task_type"] == report["task_type"]
        and report["run_id"] == frozen["run_id"] == evidence["run_id"]
        and report["frozen_digest"] == fingerprint(frozen) == evidence["frozen_digest"]
    )
    require(
        [t["identity"] for t in report["trials"]] == frozen["trials"]
        and len(report["trials"]) == len(evidence["trials"])
    )
    for index, (trial, ev) in enumerate(zip(report["trials"], evidence["trials"], strict=True), 1):
        require(
            trial["identity"]["trial_id"] == ev["trial_id"]
            and trial["evidence"] == ev["references"]
            and trial["convergence"] == ev["convergence"]
        )
        require(
            to_json_value(_evidence_files(root, root / f"attempts/{index:03d}"))
            == trial["evidence"]
        )
    for artifact in frozen["artifacts"]:
        require(artifact_digest(root, artifact["file"]) == artifact["sha256"])
    with zipfile.ZipFile(root / "artifacts/source.zip") as archive:
        sources = tuple((name, archive.read(name)) for name in archive.namelist())
        require(source_digest(sources) == frozen["variant"]["source_digest"])
    return (
        frozen,
        report,
        {
            "run_id": report["run_id"],
            "frozen_digest": report["frozen_digest"],
            "evidence_digest": evidence_file.sha256,
            "report_digest": report_file.sha256,
        },
    )
