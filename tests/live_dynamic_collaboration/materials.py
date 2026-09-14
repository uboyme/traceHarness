"""Create and check explicit, reduced DA fixtures before any model request.

This is material validation, not another task evaluator. Real comparisons use
the existing ProductTaskEvaluator and its per-case VerificationPlan unchanged.
"""

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

from live_dynamic_collaboration.cases import CASES
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def budgets(tokens, steps, tools, wall, children, depth, processes):
    return dict(
        max_tokens=tokens,
        max_steps=steps,
        max_tool_calls=tools,
        max_wall_milliseconds=wall,
        max_children=children,
        max_depth=depth,
        max_processes=processes,
    )


def build(repository, output, *, refresh=False):
    """An explicit experiment profile, never loaded as a runtime default."""
    if output.exists() and refresh:
        for split in ("development", "holdout"):
            existing = json.loads((output / split / "benchmark.json").read_text(encoding="utf-8"))
            if existing["benchmark_id"] != "traceh-da-" + split + "-v1":
                raise ValueError("material-refresh-owner-mismatch")
    else:
        output.mkdir(parents=True, exist_ok=False)
    expected = {("development", name): 4 for name in ("local", "independent", "dependent")}
    expected.update({("holdout", name): 2 for name in ("local", "independent", "dependent")})
    if Counter((case.split, case.category) for case in CASES) != expected:
        raise ValueError("material-group-count-mismatch")
    if len({case.name for case in CASES}) != len(CASES):
        raise ValueError("material-issue-group-overlap")
    template = json.loads(
        (repository / "benchmarks/product_v1/benchmark.json").read_text(encoding="utf-8")
    )
    settings = template["task_settings"]
    settings.update(
        profile_id="da-readonly-development-v1",
        default_mode="single",
        modes=["single", "adaptive"],
        retained_tokens=32000,
        max_report_chars=12000,
    )
    settings["task_budget"] = budgets(600000, 120, 160, 1800000, 4, 2, 3)
    settings["roles"]["coder"].update(
        budget=budgets(300000, 60, 80, 900000, 3, 1, 2),
        max_output_tokens=8192,
        max_turn_wall_milliseconds=300000,
    )
    settings["roles"]["investigator"].update(
        budget=budgets(60000, 16, 20, 180000, 0, 0, 0),
        max_output_tokens=4096,
        max_turn_wall_milliseconds=120000,
    )
    # Only the current single/adaptive execution contract is generated here.
    for split in ("development", "holdout"):
        root = output / split
        cases, criteria, origins = [], {}, []
        for case in (item for item in CASES if item.split == split):
            directory = root / case.name / "initial"
            directory.mkdir(parents=True, exist_ok=refresh)
            files = {
                **case.files,
                ".gitignore": "__pycache__/\n*.pyc\n",
                "CONTRACT.md": "# 用户需求\n\n" + case.requirement + "\n",
            }
            for name, body in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            verification = dict(
                plan_id="da-contract-" + case.name,
                plan_version=1,
                protocol_version=3,
                commands=[
                    dict(
                        command_id="contract",
                        public_requirement=None,
                        argv=["python", "-B", "-c", case.checks],
                        timeout_ms=30000,
                    )
                ],
                environment=dict(
                    policy_id="da-isolated-python-v1",
                    passthrough=[],
                    overrides={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                ),
                max_output_bytes=1048576,
            )
            cases.append(
                dict(
                    case_id=case.name,
                    group_id=case.name,
                    requirement=case.requirement,
                    initial_tree=directory.relative_to(root).as_posix(),
                    sha256=initial_tree_digest(capture_initial_tree(directory)),
                    verification=verification,
                )
            )
            criteria[case.name] = [
                case.requirement,
                "修改应适用于合同内一般输入，不得仅匹配示例值。",
                "只声明实际执行过的检查；不把助手报告视为独立验证。",
            ]
            sources = []
            for name in case.sources:
                path = repository / name
                sources.append(dict(file=name, sha256=digest_bytes(path.read_bytes())))
            origins.append(
                dict(
                    case_id=case.name,
                    group_id=case.name,
                    category=case.category,
                    provenance=(
                        "Reduced reconstruction of repository contracts; "
                        "not verbatim historical code."
                    ),
                    source_contracts=sources,
                )
            )
        write(root / "dataset.json", dict(format=2, cases=cases))
        write(root / "rubric.json", dict(format=1, criteria=criteria))
        manifest = dict(
            protocol_version=3,
            benchmark_id="traceh-da-" + split + "-v1",
            task_type="product_task",
            task_settings=settings,
            dataset=dict(
                file="dataset.json", sha256=digest_bytes((root / "dataset.json").read_bytes())
            ),
            assessment=dict(
                scorer_id="product-durable-semantic-v1",
                version=1,
                requires_review=True,
                rubric=dict(
                    file="rubric.json", sha256=digest_bytes((root / "rubric.json").read_bytes())
                ),
            ),
        )
        write(root / "benchmark.json", manifest)
        write(
            root / "provenance.json",
            dict(
                format=1,
                split=split,
                cases=origins,
                generator_sha256=digest_bytes(Path(__file__).read_bytes()),
                definitions_sha256=digest_bytes(Path(__file__).with_name("cases.py").read_bytes()),
            ),
        )
    return output


def validate(output, *, image, docker_context):
    """Prove faulty fixtures fail and reference repairs pass in the declared OS."""
    if not image.startswith("sha256:") or not docker_context:
        raise ValueError("explicit-local-image-and-context-required")
    records = []
    for case in CASES:
        root = output / case.split
        manifest = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
        dataset_bytes = (root / "dataset.json").read_bytes()
        if digest_bytes(dataset_bytes) != manifest["dataset"]["sha256"]:
            raise ValueError("material-dataset-drift")
        frozen = next(
            row for row in json.loads(dataset_bytes)["cases"] if row["case_id"] == case.name
        )
        initial = root / frozen["initial_tree"]
        if (
            initial_tree_digest(capture_initial_tree(initial)) != frozen["sha256"]
            or frozen["verification"]["commands"][0]["argv"] != ["python", "-B", "-c", case.checks]
            or any(
                (initial / name).read_text(encoding="utf-8") != text
                for name, text in case.files.items()
            )
        ):
            raise ValueError("material-source-or-verifier-drift")
        outcomes = []
        for repaired in (False, True):
            with TemporaryDirectory(prefix="traceh-da-material-") as temporary:
                root = Path(temporary).resolve()
                if root.parent != Path(gettempdir()).resolve():
                    raise ValueError("temporary-directory-owner-mismatch")
                files = {**case.files, **(case.repaired if repaired else {})}
                for name, body in files.items():
                    (root / name).write_text(body, encoding="utf-8")
                command = [
                    "docker",
                    "--context",
                    docker_context,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--memory",
                    "256m",
                    "--pids-limit",
                    "64",
                    "--cpus",
                    "1",
                    "--mount",
                    f"type=bind,source={root},target=/work,readonly",
                    "--workdir",
                    "/work",
                    image,
                    "python",
                    "-B",
                    "-c",
                    case.checks,
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", timeout=60
                )
                if result.returncode not in (0, 1):
                    raise RuntimeError("material-container-not-executed")
                # The expected fault must reach a contract assertion; an import,
                # syntax or startup failure is not a valid negative fixture.
                valid = result.returncode == (0 if repaired else 1)
                if not repaired:
                    valid = valid and "AssertionError" in result.stderr
                outcomes.append(
                    dict(
                        reference_repair=repaired,
                        exit_code=result.returncode,
                        contract_verified=valid,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                )
        record = dict(
            case_id=case.name,
            split=case.split,
            outcomes=outcomes,
            initial_tree_digest=frozen["sha256"],
            verification_sha256=digest_bytes(case.checks.encode("utf-8")),
        )
        records.append(record)
        print(
            json.dumps(
                {"material": case.name, "verified": all(o["contract_verified"] for o in outcomes)}
            ),
            flush=True,
        )
    document = dict(
        format=1, image=image, docker_context=docker_context, network="none", cases=records
    )
    write(output / "material-validation.json", document)
    if not all(o["contract_verified"] for r in records for o in r["outcomes"]):
        raise ValueError("material-oracle-validation-failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Explicitly regenerate these authored pre-freeze fixtures",
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--docker-context", required=True)
    options = parser.parse_args()
    if not options.validate_only:
        build(options.repository.resolve(), options.output.resolve(), refresh=options.refresh)
    validate(options.output.resolve(), image=options.image, docker_context=options.docker_context)
