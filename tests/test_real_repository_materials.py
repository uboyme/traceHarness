"""Material boundaries: complete source trees, explicit IDs, no silent aliases."""

import hashlib
import io
import json
import runpy
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from real_repository_evaluation.materials import (
    expected_tests,
    unpack,
    verifier,
    verify_upstream_tree,
)

from traceh.evaluation.repositories import InitialTreeLimits


def archive(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        for name, data, kind in members:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(data) if kind == tarfile.REGTYPE else 0
            stream.addfile(entry, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


def test_full_source_archive_preserves_binary_and_ignored_content(tmp_path):
    data = archive(
        [
            ("repo/.gitignore", b"asset.bin\n", tarfile.REGTYPE),
            ("repo/asset.bin", b"\x00\xff\r\n", tarfile.REGTYPE),
        ]
    )
    root = tmp_path / "initial"
    unpack(data, root, InitialTreeLimits(2, 20, 40))
    assert {p.name: p.read_bytes() for p in root.iterdir()} == {
        ".gitignore": b"asset.bin\n",
        "asset.bin": b"\x00\xff\r\n",
    }


@pytest.mark.parametrize(
    "name,kind",
    [
        ("repo/link", tarfile.SYMTYPE),
        ("repo/link", tarfile.LNKTYPE),
        ("repo/../../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("repo/.git/config", tarfile.REGTYPE),
    ],
)
def test_unsupported_archive_is_rejected_without_partial_source(tmp_path, name, kind):
    root = tmp_path / "initial"
    data = archive([("repo/valid", b"kept", tarfile.REGTYPE), (name, b"bad", kind)])
    with pytest.raises(ValueError):
        unpack(data, root, InitialTreeLimits(3, 20, 60))
    assert not root.exists()


def test_explicit_test_id_mapping_preserves_all_expected_tests():
    row = {"FAIL_TO_PASS": '["failing"]', "PASS_TO_PASS": '["parameter[with"]'}
    spec = {"test_id_map": {"parameter[with": "parameter[with spaces]"}}
    assert expected_tests(row, spec) == [["failing"], ["parameter[with spaces]"]]
    assert expected_tests(row, {"test_id_map": {}}) == [["failing"], ["parameter[with"]]


@pytest.mark.parametrize("mapping", [{"unknown": "x"}, {"b": "a"}])
def test_unknown_or_colliding_id_mapping_cannot_reduce_the_denominator(mapping):
    row = {"FAIL_TO_PASS": '["a"]', "PASS_TO_PASS": '["b"]'}
    with pytest.raises(ValueError):
        expected_tests(row, {"test_id_map": mapping})


def test_upstream_tree_checks_completeness_identity_and_git_blob_bytes(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    row = {"repo": "owner/repository", "base_commit": "a" * 40}
    tree = {
        "repository": row["repo"],
        "base_commit": row["base_commit"],
        "entries": [
            {
                "path": "a.py",
                "mode": "100644",
                "size": 6,
                "sha": hashlib.sha1(b"blob 6\0x = 1\n").hexdigest(),
            },
        ],
    }
    limits = InitialTreeLimits(3, 20, 60)
    assert verify_upstream_tree(tmp_path, limits, tree, row) == (("a.py", b"x = 1\n"),)
    (tmp_path / "missing-from-inventory").write_bytes(b"extra")
    with pytest.raises(ValueError, match="complete upstream tree"):
        verify_upstream_tree(tmp_path, limits, tree, row)
    (tmp_path / "missing-from-inventory").unlink()
    (tmp_path / "a.py").write_bytes(b"x = 2\n")
    with pytest.raises(ValueError, match="upstream blob mismatch"):
        verify_upstream_tree(tmp_path, limits, tree, row)


@pytest.mark.parametrize("fixed", [True, False])
def test_generated_oracle_uses_frozen_tests_despite_candidate_test_edits(tmp_path, fixed):
    # Host execution here is a unit test of our own tiny trusted fixture only.
    # Public repository tests are executed exclusively by the sandbox preflight.
    initial, hidden = tmp_path / "initial", tmp_path / "hidden"
    initial.mkdir()
    hidden.mkdir()
    original = b"from implementation import value\ndef test_rule():\n    assert value == 1\n"
    (initial / "test_rule.py").write_bytes(original)
    (hidden / "test_rule.py").write_bytes(original)
    (initial / "implementation.py").write_text(f"value = {int(fixed)}\n", encoding="utf-8")
    row = {"FAIL_TO_PASS": '["test_rule.py::test_rule"]', "PASS_TO_PASS": "[]"}
    spec = {
        "test_id_map": {},
        "protected_roots": [],
        "writable_test_patterns": [],
        "protected_files": ["test_rule.py"],
        "test_files": ["test_rule.py"],
        "import_roots": ["."],
    }
    plan = verifier(initial, hidden, row, spec)
    # Positive: a legitimate additional regression. Negative: replacing the
    # candidate test with a tautology must not hide an unfixed implementation.
    (initial / "test_rule.py").write_bytes(
        original + b"\ndef test_additional():\n    assert value == 1\n"
        if fixed else b"def test_rule():\n    assert True\n"
    )
    command = [sys.executable, *plan["commands"][0]["argv"][1:]]
    result = subprocess.run(command, cwd=initial, capture_output=True, timeout=30)
    assert result.returncode == (0 if fixed else 1)
    assert b"TRACEH_RR_RESULT=" in result.stdout
    report = json.loads(result.stdout.split(b"TRACEH_RR_RESULT=", 1)[1])
    assert report["failed"] == ([] if fixed else ["test_rule.py::test_rule"])
    assert report["passed"] == (["test_rule.py::test_rule"] if fixed else [])
    assert (initial / "test_rule.py").read_bytes() == original


def test_generated_oracle_still_refuses_changed_support_material(tmp_path):
    initial, hidden = tmp_path / "initial", tmp_path / "hidden"
    initial.mkdir()
    hidden.mkdir()
    original = b"def test_rule():\n    assert True\n"
    (initial / "test_rule.py").write_bytes(original)
    (hidden / "test_rule.py").write_bytes(original)
    (initial / "conftest.py").write_bytes(b"# frozen support\n")
    row = {"FAIL_TO_PASS": '["test_rule.py::test_rule"]', "PASS_TO_PASS": "[]"}
    spec = dict(test_id_map={}, protected_roots=[],
                writable_test_patterns=[],
                protected_files=["test_rule.py", "conftest.py"],
                test_files=["test_rule.py"], import_roots=["."])
    plan = verifier(initial, hidden, row, spec)
    (initial / "conftest.py").write_bytes(b"raise RuntimeError('candidate support')\n")
    result = subprocess.run(
        [sys.executable, *plan["commands"][0]["argv"][1:]],
        cwd=initial, capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    assert b"protected test material changed: conftest.py" in result.stderr
    assert b"TRACEH_RR_RESULT=" not in result.stdout


def test_generated_oracle_accepts_unselected_regression_but_not_support_tampering(tmp_path):
    initial, hidden = tmp_path / "initial", tmp_path / "hidden"
    for root in (initial, hidden):
        (root / "tests").mkdir(parents=True)
        (root / "tests/test_selected.py").write_bytes(
            b"from implementation import value\ndef test_rule():\n    assert value == 1\n"
        )
    (initial / "implementation.py").write_bytes(b"value = 1\n")
    (initial / "tests/test_extra.py").write_bytes(b"def test_old():\n    assert True\n")
    (initial / "tests/conftest.py").write_bytes(b"# frozen support\n")
    (initial / "tests/test_data").mkdir()
    (initial / "tests/test_data/helper.py").write_bytes(b"# frozen nested support\n")
    spec = dict(
        test_id_map={}, protected_roots=["tests"], protected_files=[],
        writable_test_patterns=["tests/test_*.py"],
        test_files=["tests/test_selected.py"], import_roots=["."],
    )
    row = {"FAIL_TO_PASS": '["tests/test_selected.py::test_rule"]', "PASS_TO_PASS": "[]"}
    plan = verifier(initial, hidden, row, spec)
    command = [sys.executable, *plan["commands"][0]["argv"][1:]]

    (initial / "tests/test_extra.py").write_bytes(
        b"def test_additional():\n    assert True\n"
    )
    positive = subprocess.run(command, cwd=initial, capture_output=True, timeout=30)
    assert positive.returncode == 0
    assert b"TRACEH_RR_RESULT=" in positive.stdout

    (initial / "implementation.py").write_bytes(b"value = 0\n")
    unfixed = subprocess.run(command, cwd=initial, capture_output=True, timeout=30)
    assert unfixed.returncode == 1
    assert b"tests/test_selected.py::test_rule" in unfixed.stdout

    (initial / "tests/conftest.py").write_bytes(b"raise RuntimeError('candidate support')\n")
    tampered = subprocess.run(command, cwd=initial, capture_output=True, timeout=30)
    assert tampered.returncode == 1
    assert b"protected test material changed: tests/conftest.py" in tampered.stderr
    assert b"TRACEH_RR_RESULT=" not in tampered.stdout

    (initial / "tests/conftest.py").write_bytes(b"# frozen support\n")
    (initial / "tests/test_data/helper.py").write_bytes(b"# candidate nested support\n")
    nested = subprocess.run(command, cwd=initial, capture_output=True, timeout=30)
    assert nested.returncode == 1
    assert b"protected test material changed: tests/test_data/helper.py" in nested.stderr


@pytest.mark.parametrize(
    "variant,complete,status,exit_code",
    [
        ("reference", True, "passed", 0),
        ("reference", False, "passed", 1),
        ("reference", True, "failed", 1),
        ("unfixed", True, "failed", 0),
    ],
)
def test_acceptance_cli_reports_measurement_and_expected_outcome(
    tmp_path, monkeypatch, variant, complete, status, exit_code
):
    # Exercise the script's public exit status, not a private predicate. The
    # original Runner result is the only authority; this test makes no model call.
    class RecordedRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return SimpleNamespace(
                complete=complete,
                trials=[SimpleNamespace(assessment=SimpleNamespace(value=status))],
            )

    monkeypatch.setattr("traceh.evaluation.runner.EvaluationRunner", RecordedRunner)
    monkeypatch.setattr(
        "traceh.sandbox.config.load_sandbox_file", lambda path: SimpleNamespace(policy=None)
    )
    monkeypatch.setattr(
        sys, "argv", [
            "acceptance", "--materials", str(tmp_path), "--sandbox", str(tmp_path / "sandbox"),
            "--output", str(tmp_path / "out"), "--variant", variant,
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_module("real_repository_evaluation.acceptance", run_name="__main__")
    assert result.value.code == exit_code
