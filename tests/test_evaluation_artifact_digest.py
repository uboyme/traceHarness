"""Binary evidence uses bounded-memory hashing, not the JSON document quota."""

import hashlib
import json

import pytest
from test_evaluation_comparison import run_pair

from traceh.evaluation import inputs
from traceh.evaluation.comparison import compare_experiment
from traceh.evaluation.errors import BenchmarkManifestError


def test_binary_digest_keeps_document_limit_and_rejects_missing_or_outside(tmp_path):
    payload = b"explicit binary fixture\x00" * (inputs.MAX_DOCUMENT_BYTES // 20 + 1)
    path = tmp_path / "artifact.zip"
    path.write_bytes(payload)
    assert len(payload) > inputs.MAX_DOCUMENT_BYTES
    assert inputs.artifact_digest(tmp_path, path.name) == hashlib.sha256(payload).hexdigest()
    with pytest.raises(BenchmarkManifestError):
        inputs.read_input(tmp_path, path.name)
    for name in ("missing.zip", "../artifact.zip"):
        with pytest.raises(BenchmarkManifestError):
            inputs.artifact_digest(tmp_path, name)


async def test_public_comparison_binary_quota_and_tamper_rejection(tmp_path, monkeypatch):
    root, code = await run_pair(tmp_path)
    assert code == 0
    # Keep all actual JSON readable while making genuine frozen ZIPs exceed
    # the document quota. Both the experiment and per-arm evidence are checked.
    json_limit = max(p.stat().st_size for p in root.rglob("*.json")) + 1
    archive = root / "artifacts/base-source.zip"
    assert archive.stat().st_size > json_limit
    monkeypatch.setattr(inputs, "MAX_DOCUMENT_BYTES", json_limit)
    report = compare_experiment(root, tmp_path / "offline-recheck")
    assert report["complete"], report
    assert report["hard_constraints"] == "passed"
    assert report["planned_pairs"] == 1
    assert json.loads((root / "comparison/report.json").read_bytes())["complete"]
    with archive.open("ab") as handle:
        handle.write(b"explicit tampering")
    rejected = compare_experiment(root, tmp_path / "offline-rejected")
    assert not rejected["complete"]
    assert rejected["status"] == "not_comparable"
    assert rejected["reason"] == "evaluation-comparison-incompatible"
