"""Build the task-type benchmark materials from one verified upstream tree (plan S0-E).

The pristine tree is unpacked from the cached SWE-bench Lite archive and checked
blob by blob against the committed upstream inventory, exactly as the real
repository materials are. Every case then applies exact, single-occurrence text
edits. Hidden tests and reference trees stay under ``host/``; only the injected
initial tree becomes Agent-visible.

``calibration`` is the frozen per-case list of fail-to-pass and pass-to-pass test
IDs produced by :mod:`calibrate`; without it the builder only prepares the trees
and hidden files needed to calibrate, and writes no dataset.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import shutil
import zlib
from pathlib import Path, PurePosixPath

from real_repository_evaluation.materials import sha, unpack, verify_upstream_tree, write_json

from task_type_evaluation.spec import CASES, UPSTREAM
from traceh.evaluation.repositories import (
    InitialTreeLimits,
    capture_initial_tree,
    initial_tree_digest,
)

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmarks" / "task_type_v1"
RR = ROOT / "benchmarks" / "real_repository_v1"
ORACLE = ROOT / "tests" / "real_repository_evaluation" / "oracle.py"


def spec_digest(hidden=None):
    """Identity of every host-owned input that defines the cases.

    Only source files count: a bytecode cache next to a hidden test is neither
    committed nor part of the case, and once made the digest differ between a
    working tree and a clean clone. Line endings are normalised so a checkout
    that converts them cannot change the identity either.
    """
    hidden = BENCHMARK / "hidden" if hidden is None else Path(hidden)

    def text(path):
        return sha(path.read_bytes().replace(b"\r\n", b"\n"))

    entries = [
        ["spec.py", text(Path(__file__).with_name("spec.py"))],
        ["oracle.py", text(ORACLE)],
    ]
    entries += [
        ["hidden/" + p.relative_to(hidden).as_posix(), text(p)]
        for p in sorted(hidden.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.name.endswith((".py", ".py.in"))
    ]
    return sha(json.dumps(entries).encode("utf-8"))


def apply_edits(root, edits):
    for edit in edits:
        path = root / edit["file"]
        text = path.read_text(encoding="utf-8")
        if text.count(edit["old"]) != 1:
            raise ValueError("edit anchor is not unique: " + edit["file"])
        path.write_bytes(text.replace(edit["old"], edit["new"]).encode("utf-8"))


def _callable_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Lambda):
        return "<lambda>"
    if isinstance(node, ast.Call) and _callable_name(node.func) == "inference_tip":
        return _callable_name(node.args[0])
    raise ValueError("unsupported registration argument")


def registry(source):
    """The host's own answer to the a2 question, from the syntax tree only."""
    tree = ast.parse(source)
    functions = sorted(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    transforms, extenders = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        named = isinstance(node.func, ast.Name | ast.Attribute)
        callee = _callable_name(node.func) if named else None
        if callee == "register_transform":
            transforms.append(
                {
                    "node_class": _callable_name(node.args[0]),
                    "transform": _callable_name(node.args[1]),
                }
            )
        elif callee == "register_module_extender":
            if not isinstance(node.args[1], ast.Constant):
                raise ValueError("module extender name is not a literal")
            extenders.append(node.args[1].value)
    return {"functions": functions, "transforms": transforms, "module_extenders": extenders}


def hidden_files(case, pristine):
    files = {}
    for name in case["hidden"]["new"]:
        source = BENCHMARK / "hidden" / case["case_id"] / name
        if source.exists():
            files[name] = source.read_bytes()
        else:
            template = source.with_name(source.name + ".in").read_text(encoding="utf-8")
            expected = {
                module: registry((pristine / module).read_text(encoding="utf-8"))
                for module in case["reference_findings"]
            }
            rendered = template.replace("__EXPECTED__", json.dumps(expected, sort_keys=True))
            files[name] = rendered.encode("utf-8")
    for name in case["hidden"]["pristine"]:
        files[name] = (pristine / name).read_bytes()
    return files


def reference_tree(case, pristine, destination):
    shutil.copytree(pristine, destination)
    apply_edits(destination, case["reference"])
    if case.get("reference_findings"):
        answer = {
            m: registry((pristine / m).read_text(encoding="utf-8"))
            for m in case["reference_findings"]
        }
        (destination / "FINDINGS.json").write_text(
            json.dumps(answer, indent=2, sort_keys=True), encoding="utf-8"
        )


def verifier(case, initial, tests, fail_to_pass, pass_to_pass):
    protected = {}
    for name in case["protected_roots"]:
        for path in sorted((initial / name).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(initial).as_posix()
            if any(PurePosixPath(relative).match(p) for p in case["writable_test_patterns"]):
                continue
            protected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in case["protected_files"]:
        protected[name] = hashlib.sha256((initial / name).read_bytes()).hexdigest()
    payload = {
        "protected": protected,
        "test_files": {n: base64.b64encode(c).decode("ascii") for n, c in sorted(tests.items())},
        "import_roots": case["import_roots"],
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
    }
    packed = base64.b64encode(zlib.compress(json.dumps(payload).encode("utf-8"), 9)).decode()
    argv = ["python", "-I", "-B", "-c", ORACLE.read_text(encoding="utf-8")]
    argv += [packed[i : i + 3000] for i in range(0, len(packed), 3000)]
    return dict(
        protocol_version=3,
        plan_id="task-type-regression",
        plan_version=1,
        commands=[
            dict(
                command_id="hidden-tests",
                argv=argv,
                timeout_ms=170000,
                public_requirement="Satisfy the requirement and preserve existing behaviour.",
            )
        ],
        environment=dict(
            policy_id="task-type-offline-python",
            passthrough=[],
            overrides={
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
        ),
        max_output_bytes=1048576,
    )


def pristine_tree(cache, output):
    selection = json.loads((RR / "selection.json").read_bytes())
    spec = next(t for t in selection["tasks"] if t["instance_id"] == UPSTREAM)
    row_bytes = (RR / spec["instance_file"]).read_bytes()
    if sha(row_bytes) != spec["instance_sha256"]:
        raise ValueError("instance digest mismatch")
    row = json.loads(row_bytes)
    archive = (cache / (spec["archive_sha256"] + ".tar.gz")).read_bytes()
    if sha(archive) != spec["archive_sha256"]:
        raise ValueError("cached archive digest mismatch")
    limits = InitialTreeLimits(**spec["initial_tree_limits"])
    pristine = output / "pristine"
    unpack(archive, pristine, limits)
    tree = (RR / spec["tree_file"]).read_bytes()
    if sha(tree) != spec["tree_sha256"]:
        raise ValueError("upstream tree digest mismatch")
    verify_upstream_tree(pristine, limits, json.loads(tree), row)
    return pristine, spec, limits


def prepare(cache, output, *, calibration=None, template=None):
    output.mkdir(parents=True, exist_ok=False)
    pristine, upstream, limits = pristine_tree(cache, output)
    digest = spec_digest()
    frozen = None
    if calibration is not None:
        frozen = json.loads(calibration.read_bytes())
        if frozen["spec_digest"] != digest:
            raise ValueError("calibration belongs to another case definition")
    cases = []
    for case in CASES:
        name = case["case_id"]
        initial = output / "material" / name / "initial"
        shutil.copytree(pristine, initial)
        apply_edits(initial, case["inject"])
        host = output / "host" / name
        reference_tree(case, pristine, host / "reference")
        tests = hidden_files(case, pristine)
        for path, content in tests.items():
            target = host / "tests" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        write_json(host / "case.json", {k: v for k, v in case.items() if k != "reference"})
        if frozen is None:
            continue
        ids = frozen["cases"][name]
        captured = capture_initial_tree(initial, limits=limits)
        cases.append(
            dict(
                case_id=name,
                group_id="task-type-" + case["category"],
                requirement=case["requirement"],
                initial_tree=f"{name}/initial",
                initial_tree_limits=upstream["initial_tree_limits"],
                sha256=initial_tree_digest(captured),
                verification=verifier(
                    case, initial, tests, ids["fail_to_pass"], ids["pass_to_pass"]
                ),
            )
        )
    write_json(
        output / "provenance.json",
        dict(
            upstream=UPSTREAM,
            upstream_archive_sha256=upstream["archive_sha256"],
            spec_digest=digest,
            calibration=None if calibration is None else sha(calibration.read_bytes()),
            execution_kind="not-yet-executed",
        ),
    )
    if frozen is None:
        return output
    root = output / "material"
    write_json(root / "dataset.json", dict(format=3, cases=cases))
    manifest = json.loads(template.read_bytes())
    manifest["benchmark_id"] = "traceh-task-type-v1"
    dataset = sha((root / "dataset.json").read_bytes())
    manifest["dataset"] = dict(file="dataset.json", sha256=dataset)
    write_json(root / "benchmark.json", manifest)
    from traceh.evaluation.evaluators.product_manifest import load_product_suite
    from traceh.evaluation.manifest import load_benchmark_manifest

    load_product_suite(load_benchmark_manifest(root), provider_id="offline", model_id="offline")
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--template", type=Path)
    options = parser.parse_args()
    if (options.calibration is None) != (options.template is None):
        parser.error("--calibration and --template are given together")
    print(
        prepare(
            options.cache.resolve(),
            options.output.resolve(),
            calibration=options.calibration and options.calibration.resolve(),
            template=options.template and options.template.resolve(),
        )
    )
