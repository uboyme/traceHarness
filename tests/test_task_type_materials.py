"""Task-type materials: frozen case identity, host-computed answers, exact edits."""

import json
import subprocess
import sys

import pytest
from task_type_evaluation.build import (
    BENCHMARK,
    apply_edits,
    hidden_files,
    registry,
    spec_digest,
    verifier,
)
from task_type_evaluation.spec import CASES, READ_MODULES


def test_the_frozen_calibration_belongs_to_the_current_case_definition():
    # Editing a case, hidden test or the oracle without recalibrating would
    # silently score new tasks with old expectations; the builder refuses that,
    # and this keeps the committed calibration honest.
    calibration = json.loads((BENCHMARK / "calibration.json").read_bytes())
    assert calibration["spec_digest"] == spec_digest()
    assert set(calibration["cases"]) == {case["case_id"] for case in CASES}
    assert all(entry["fail_to_pass"] for entry in calibration["cases"].values())


def test_five_categories_each_pre_register_an_expectation():
    categories = [case["category"] for case in CASES]
    assert len(set(categories)) == len(categories) == 5
    assert all(case["expected"] for case in CASES)
    for case in CASES:
        # A fix task must change the Agent's tree; a feature task must have a reference.
        assert case["inject"] or case["reference"] or case.get("reference_findings")
        for name in case["hidden"]["new"]:
            source = BENCHMARK / "hidden" / case["case_id"] / name
            assert source.exists() or source.with_name(source.name + ".in").exists()


def test_registry_answers_come_from_the_syntax_tree():
    source = (
        "from astroid.manager import AstroidManager\n"
        "def _transform(node): pass\n"
        "def _looks(node): return True\n"
        "if True:\n    def hidden(): pass\n"
        "AstroidManager().register_transform(Call, inference_tip(_transform), _looks)\n"
        "AstroidManager().register_transform(nodes.ClassDef, lambda n: n)\n"
        "register_module_extender(AstroidManager(), 'crypt', _transform)\n"
    )
    assert registry(source) == {
        "functions": ["_looks", "_transform"],
        "transforms": [
            {"node_class": "Call", "transform": "_transform"},
            {"node_class": "ClassDef", "transform": "<lambda>"},
        ],
        "module_extenders": ["crypt"],
    }
    with pytest.raises(ValueError, match="unsupported registration"):
        registry("m.register_transform(Call, handlers[0])\n")


def test_the_a2_checker_embeds_the_host_answer(tmp_path):
    case = next(c for c in CASES if c.get("reference_findings"))
    for module in READ_MODULES:
        path = tmp_path / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def only_{path.stem}(): pass\n", encoding="utf-8")
    rendered = hidden_files(case, tmp_path)["tests/test_task_findings.py"].decode("utf-8")
    assert "__EXPECTED__" not in rendered
    assert '"functions": ["only_brain_crypt"]' in rendered


def test_edits_must_match_exactly_once(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not unique"):
        apply_edits(tmp_path, [{"file": "m.py", "old": "x = 1\n", "new": "x = 2\n"}])
    with pytest.raises(ValueError, match="not unique"):
        apply_edits(tmp_path, [{"file": "m.py", "old": "y = 1\n", "new": "y = 2\n"}])
    apply_edits(tmp_path, [{"file": "m.py", "old": "x = 1\nx", "new": "x = 0\nx"}])
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "x = 0\nx = 1\n"


@pytest.mark.parametrize("fixed", [False, True])
def test_the_packed_plan_runs_the_hidden_tests_and_protects_support_files(tmp_path, fixed):
    # Host execution here is a unit test of a tiny trusted fixture only; the real
    # cases run exclusively through the production sandbox calibration/admission.
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    (root / "setup.cfg").write_text("[x]\n", encoding="utf-8")
    (root / "tests" / "helper.py").write_text("SUPPORT = 1\n", encoding="utf-8")
    (root / "impl.py").write_text(f"value = {int(fixed)}\n", encoding="utf-8")
    case = {
        "protected_roots": ["tests"],
        "writable_test_patterns": ["tests/test_*.py"],
        "protected_files": ["setup.cfg"],
        "import_roots": ["."],
    }
    hidden = {"tests/test_hidden.py": b"from impl import value\ndef test_it():\n    assert value\n"}
    plan = verifier(case, root, hidden, ["tests/test_hidden.py::test_it"], [])
    # The candidate may add its own test file; it is not what decides the result.
    (root / "tests" / "test_candidate.py").write_text("def test_x(): pass\n", encoding="utf-8")
    command = [sys.executable, *plan["commands"][0]["argv"][1:]]
    result = subprocess.run(command, cwd=root, capture_output=True, timeout=60)
    assert result.returncode == (0 if fixed else 1)
    report = json.loads(result.stdout.split(b"TRACEH_RR_RESULT=", 1)[1])
    assert report["passed" if fixed else "failed"] == ["tests/test_hidden.py::test_it"]
    # Tampering with protected support material is refused before any test runs.
    (root / "tests" / "helper.py").write_text("SUPPORT = 2\n", encoding="utf-8")
    refused = subprocess.run(command, cwd=root, capture_output=True, timeout=60)
    assert refused.returncode != 0 and b"protected test material changed" in refused.stderr


def test_the_case_identity_ignores_caches_and_line_endings(tmp_path):
    import shutil

    hidden = tmp_path / "hidden"
    shutil.copytree(BENCHMARK / "hidden", hidden)
    before = spec_digest(hidden)
    # A clean clone has neither bytecode caches nor necessarily the same line endings.
    cache = hidden / "a1-small-hashlib-blake2" / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "test_task_hashlib_blake2.cpython-312.pyc").write_bytes(b"\0\r\nstale")
    target = next(hidden.rglob("test_task_three_brains.py"))
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert spec_digest(hidden) == before
    # A real edit of a hidden test still changes the identity.
    target.write_bytes(target.read_bytes() + b"# changed\n")
    assert spec_digest(hidden) != before
