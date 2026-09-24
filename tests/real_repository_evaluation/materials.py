"""Prepare full, pinned public trees for the existing ProductTask evaluator.

Reference answers stay outside initial trees. No task execution or scoring owner
is added here: this is an explicit material producer, like the other live helpers.
"""

import argparse
import base64
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import urllib.request
import zlib
from pathlib import Path, PurePosixPath

from traceh.evaluation.repositories import (
    InitialTreeLimits,
    capture_initial_tree,
    initial_tree_digest,
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected_tests(row, specification):
    groups = [json.loads(row[key]) for key in ("FAIL_TO_PASS", "PASS_TO_PASS")]
    mapping = specification["test_id_map"]
    if not set(mapping) <= set(groups[0] + groups[1]):
        raise ValueError("test ID map names an unknown upstream ID")
    resolved = [[mapping.get(node, node) for node in group] for group in groups]
    if len(set(resolved[0] + resolved[1])) != len(resolved[0] + resolved[1]):
        raise ValueError("test ID mapping is ambiguous")
    return resolved


def unpack(data, destination, limits):
    """Extract all ordinary members or refuse; never trim or follow a link."""
    entries = {}
    total = 0
    prefixes = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or any(":" in part for part in path.parts)
            ):
                raise ValueError("unsafe archive path")
            prefixes.add(path.parts[0])
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError("archive contains unsupported links or special files")
            relative = Path(*path.parts[1:])
            if any(part in {".git", ".traceh"} for part in relative.parts):
                raise ValueError("archive contains reserved paths")
            if relative in entries:
                raise ValueError("duplicate archive member")
            total += member.size
            if (
                len(entries) >= limits.max_files
                or member.size > limits.max_file_bytes
                or total > limits.max_total_bytes
            ):
                raise ValueError("archive exceeds explicit material limits")
            content = archive.extractfile(member).read(limits.max_file_bytes + 1)
            if len(content) != member.size:
                raise ValueError("archive member size mismatch")
            entries[relative] = content
    if len(prefixes) != 1 or not entries:
        raise ValueError("archive must contain one nonempty source tree")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in entries.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def apply_patch(root, patch):
    # A private Git directory prevents discovery of the caller's repository.
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, timeout=30)
    for check in (True, False):
        argv = ["git", "-C", str(root), "apply", "--whitespace=nowarn"]
        if check:
            argv.append("--check")
        subprocess.run(argv + ["-"], input=patch.encode("utf-8"), check=True, timeout=30)


def verify_upstream_tree(initial, limits, tree, row):
    """Prove codeload did not omit export-ignored or otherwise tracked files."""
    if tree["repository"] != row["repo"] or tree["base_commit"] != row["base_commit"]:
        raise ValueError("upstream tree identity mismatch")
    frozen = capture_initial_tree(initial, limits=limits)
    expected = {item["path"]: item for item in tree["entries"]}
    if len(expected) != len(tree["entries"]) or set(expected) != {name for name, _ in frozen}:
        raise ValueError("archive is not the complete upstream tree")
    for name, content in frozen:
        item = expected[name]
        blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
        if (
            item["mode"] not in {"100644", "100755"}
            or item["size"] != len(content)
            or hashlib.sha1(blob).hexdigest() != item["sha"]
        ):
            raise ValueError("upstream blob mismatch")
    return frozen


def verifier(initial, patched, row, specification):
    fail_to_pass, pass_to_pass = expected_tests(row, specification)
    protected = {}
    for name in specification["protected_roots"]:
        for path in (initial / name).rglob("*"):
            if path.is_file():
                relative = path.relative_to(initial).as_posix()
                # Ordinary candidate regression tests outside the frozen run set
                # do not decide the result. Keep fixtures and other support files
                # protected unless the case explicitly permits their path.
                if any(
                    PurePosixPath(relative).match(pattern)
                    for pattern in specification["writable_test_patterns"]
                ):
                    continue
                protected[relative] = sha(path.read_bytes())
    for name in specification["protected_files"]:
        protected[name] = sha((initial / name).read_bytes())
    payload = {
        "protected": protected,
        "test_files": {
            name: base64.b64encode((patched / name).read_bytes()).decode("ascii")
            for name in specification["test_files"]
        },
        "import_roots": specification["import_roots"],
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
    }
    packed = base64.b64encode(zlib.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")
    program = Path(__file__).with_name("oracle.py").read_text(encoding="utf-8")
    argv = ["python", "-I", "-B", "-c", program]
    argv += [packed[index : index + 3000] for index in range(0, len(packed), 3000)]
    # Production validation below checks both per-argument and argument-count bounds.
    return dict(
        protocol_version=3,
        plan_id="real-repository-regression",
        plan_version=3,
        commands=[
            dict(
                command_id="upstream-tests",
                argv=argv,
                timeout_ms=180000,
                public_requirement="Fix the reported defect and preserve upstream tests.",
            )
        ],
        environment=dict(
            policy_id="rr-offline-python",
            passthrough=[],
            overrides={
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
        ),
        max_output_bytes=1048576,
    )


def prepare(selection_path, cache, output, template):
    selection = json.loads(selection_path.read_bytes())
    assert selection["format"] == 1
    output.mkdir(parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    cases, provenance = [], []
    for spec in selection["tasks"]:
        data = (selection_path.parent / spec["instance_file"]).read_bytes()
        if sha(data) != spec["instance_sha256"]:
            raise ValueError("instance digest mismatch")
        row = json.loads(data)
        assert row["instance_id"] == spec["instance_id"]
        archive = cache / (spec["archive_sha256"] + ".tar.gz")
        if not archive.exists():
            with urllib.request.urlopen(spec["archive_url"], timeout=60) as response:
                data = response.read(spec["archive_max_bytes"] + 1)
            if len(data) > spec["archive_max_bytes"] or sha(data) != spec["archive_sha256"]:
                raise ValueError("archive size or digest mismatch")
            archive.write_bytes(data)
        data = archive.read_bytes()
        if sha(data) != spec["archive_sha256"]:
            raise ValueError("cached archive digest mismatch")
        name = row["instance_id"]
        initial = output / "material" / name / "initial"
        limits = InitialTreeLimits(**spec["initial_tree_limits"])
        unpack(data, initial, limits)
        tree_data = (selection_path.parent / spec["tree_file"]).read_bytes()
        if sha(tree_data) != spec["tree_sha256"]:
            raise ValueError("upstream tree digest mismatch")
        frozen = verify_upstream_tree(initial, limits, json.loads(tree_data), row)
        oracle = output / "host" / name / "oracle"
        shutil.copytree(initial, oracle)
        apply_patch(oracle, row["test_patch"])
        reference = output / "host" / name / "reference"
        shutil.copytree(initial, reference)
        apply_patch(reference, row["patch"])
        # No gold patch or hidden test is copied into initial/.
        write_json(output / "host" / name / "instance.json", row)
        cases.append(
            dict(
                case_id=name,
                group_id=row["repo"].replace("/", "--"),
                requirement=row["problem_statement"].strip(),
                initial_tree=f"{name}/initial",
                initial_tree_limits=spec["initial_tree_limits"],
                sha256=initial_tree_digest(frozen),
                verification=verifier(initial, oracle, row, spec),
            )
        )
        provenance.append(
            dict(
                instance_id=name,
                repo=row["repo"],
                base_commit=row["base_commit"],
                files=len(frozen),
                bytes=sum(len(data) for _, data in frozen),
                initial_digest=initial_tree_digest(frozen),
                source=spec,
            )
        )
    root = output / "material"
    write_json(root / "dataset.json", dict(format=3, cases=cases))
    manifest = json.loads(template.read_bytes())
    manifest["benchmark_id"] = "traceh-real-repository-dev-v1"
    manifest["task_settings"]["modes"] = ["single"]
    manifest["dataset"] = dict(
        file="dataset.json", sha256=sha((root / "dataset.json").read_bytes())
    )
    write_json(root / "benchmark.json", manifest)
    write_json(
        output / "provenance.json",
        dict(selection=selection, cases=provenance, execution_kind="not-yet-executed"),
    )
    from traceh.evaluation.evaluators.product_manifest import load_product_suite
    from traceh.evaluation.manifest import load_benchmark_manifest

    load_product_suite(load_benchmark_manifest(root), provider_id="offline", model_id="offline")
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("selection", "cache", "output", "template"):
        parser.add_argument("--" + argument, type=Path, required=True)
    options = parser.parse_args()
    print(
        prepare(
            options.selection.resolve(),
            options.cache.resolve(),
            options.output.resolve(),
            options.template.resolve(),
        )
    )
