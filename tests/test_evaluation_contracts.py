"""UE-0/UE-1 admission through public loaders and the real evaluation entry point."""

import json

import pytest
from evaluation_fixtures import write_dataset
from test_product_benchmark_e2e import PRODUCT_MODEL_ID, _ProductProvider, build_benchmark

from traceh.cli.errors import CliConfigurationError
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.plan import RunOptions, load_run_options
from traceh.evaluation.runner import EvaluationRunner


def plan_file(root, benchmark, **changes):
    data = {
        "format": 1,
        "benchmark_digest": digest_bytes((benchmark / "benchmark.json").read_bytes()),
        "variants": [{"variant_id": "explicit-experiment", "role": "current", "source": "current"}],
        "model": {
            "provider": "scripted",
            "model": "fixture-model",
            "base_url": None,
            "api_key_env": "UNUSED_TEST_KEY",
            "script": None,
            "retry_policy": {
                "max_attempts": 1,
                "max_elapsed_seconds": 0,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
                "retry_after_cap_seconds": 0,
                "jitter_ratio": 0,
            },
            "timeout_seconds": 120,
        },
        "execution": {"sandbox_config": None, "max_trials": 10, "timeout_seconds": 120},
        "trials": {"repetitions": 1},
        "comparison": None,
    }
    data.update(changes)
    path = root / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("version", [1, 2, True, None, 999])
def test_old_and_unknown_envelopes_are_rejected_before_dataset(tmp_path, version):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = root / "benchmark.json"
    data = json.loads(path.read_text())
    data["protocol_version"] = version
    data["dataset"] = {"file": "absent", "sha256": ""}
    path.write_text(json.dumps(data))
    with pytest.raises(BenchmarkManifestError) as raised:
        load_benchmark_manifest(root)
    assert raised.value.code == "evaluation-version-unsupported"


@pytest.mark.parametrize("task_type", ["module:Callback", "mcp_tool", None])
def test_later_evaluators_do_not_have_placeholder_execution(tmp_path, task_type):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = root / "benchmark.json"
    data = json.loads(path.read_text())
    data["task_type"] = task_type
    path.write_text(json.dumps(data))
    with pytest.raises(BenchmarkManifestError) as raised:
        EvaluationRunner(
            root, tmp_path / "out", provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID
        )
    assert raised.value.code == "evaluation-task-type-unsupported"
    assert not (tmp_path / "out").exists()


def test_duplicate_json_keys_cannot_redefine_the_frozen_version(tmp_path):
    (tmp_path / "benchmark.json").write_text('{"protocol_version":3,"protocol_version":2}')
    with pytest.raises(BenchmarkManifestError):
        load_benchmark_manifest(tmp_path)


@pytest.mark.parametrize("what", ["manifest", "dataset", "tree"])
async def test_changed_inputs_never_start_an_attempt(tmp_path, what):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    requests = []
    runner = EvaluationRunner(
        root, tmp_path / "out", provider=_ProductProvider(requests), model_id=PRODUCT_MODEL_ID
    )
    path = {
        "manifest": root / "benchmark.json",
        "dataset": root / "dataset.json",
        "tree": root / "write_expected_file/initial/kept.txt",
    }[what]
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(BenchmarkManifestError) as raised:
        await runner.run()
    assert raised.value.code == "evaluation-frozen-input-drift"
    assert not requests and not (tmp_path / "out").exists()


def test_dataset_copy_and_identity_are_detached_from_mutable_callers(tmp_path):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    loaded = load_benchmark_manifest(root)
    data = loaded.task_settings
    data["modes"].append("multi")
    assert loaded.task_settings["modes"] == ["single"]
    cases = loaded.dataset.data["cases"]
    cases.append(cases[0])
    document = loaded.document.data
    write_dataset(root, document, cases, format_version=3)
    with pytest.raises(BenchmarkManifestError) as raised:
        EvaluationRunner(
            root, tmp_path / "out", provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID
        )
    assert raised.value.code == "benchmark-manifest-task-duplicate"


@pytest.mark.parametrize("count", [0, True, -1, 26])
def test_invalid_repeat_does_not_become_a_default(count):
    with pytest.raises(BenchmarkManifestError):
        RunOptions(repetitions=count)


def test_plan_freezes_mode_and_replication_separately(tmp_path):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1), ("multi", 1)))
    runner = EvaluationRunner(
        root,
        tmp_path / "out",
        provider=_ProductProvider(),
        model_id=PRODUCT_MODEL_ID,
        options=RunOptions(repetitions=2),
    )
    assert [(t.requested_mode, t.replicate, t.variant_id) for t in runner.trials] == [
        ("single", 1, "current"),
        ("single", 2, "current"),
        ("multi", 1, "current"),
        ("multi", 2, "current"),
    ]
    assert len({t.trial_id for t in runner.trials}) == 4
    with pytest.raises(BenchmarkManifestError) as raised:
        EvaluationRunner(
            root,
            tmp_path / "out",
            provider=_ProductProvider(),
            model_id=PRODUCT_MODEL_ID,
            options=RunOptions(max_trials=1),
        )
    assert raised.value.code == "evaluation-trial-limit"


def test_explicit_plan_nulls_do_not_inherit_environment_configuration(tmp_path):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = plan_file(tmp_path, root)
    options = load_run_options(path)
    assert options.variant_id == "explicit-experiment"
    empty = tmp_path / "empty.env"
    empty.write_text("")
    args = build_parser().parse_args(
        [
            "eval",
            str(root),
            "--output",
            str(tmp_path / "out"),
            "--run-plan",
            str(path),
            "--env-file",
            str(empty),
        ]
    )
    _configure_from_environment(
        args, environment={"TRACEH_BASE_URL": "https://unused.invalid", "TRACEH_MODEL": "wrong"}
    )
    assert args.base_url is None and args.model == "fixture-model"
    assert args.model_retry_max_attempts == 1


def test_the_provider_request_timeout_is_a_host_setting_with_a_kept_default(tmp_path):
    """The longest call a Turn makes is the one that has to deliver.

    120 seconds suits a Step that calls a tool, and it was the only value
    available: the adapter's default had no flag and no variable behind it. A
    trial then lost its assistant twice over - the final report took 71 seconds
    while the provider was fast, and when the same provider slowed to 2.3x its
    median the equivalent report ran past 120 seconds, timed out, and nothing
    was delivered. The value is stated by the host rather than derived from the
    output ceiling, which would mean assuming a rate for an unmeasured model.
    """

    from traceh.cli.main import CliConfigurationError, _provider_and_model

    def resolved(*extra, environment=None):
        args = build_parser().parse_args(
            [
                "eval",
                str(tmp_path / "b"),
                "--output",
                str(tmp_path / "out"),
                "--provider",
                "openai-compatible",
                "--model",
                "m",
                "--base-url",
                "https://unused.invalid",
                *extra,
            ]
        )
        _configure_from_environment(args, environment=environment or {})
        return _provider_and_model(args)[0]

    # Unchanged unless the host says otherwise, and it reaches the adapter.
    assert resolved().timeout_seconds == 120.0
    assert resolved("--model-timeout-seconds", "600").timeout_seconds == 600.0
    assert (
        resolved(environment={"TRACEH_MODEL_TIMEOUT_SECONDS": "300"}).timeout_seconds == 300.0
    )

    # Zero would fail every call before it is sent, so it is refused outright
    # rather than accepted as a non-negative number.
    with pytest.raises(CliConfigurationError, match="greater than zero"):
        resolved("--model-timeout-seconds", "0")


@pytest.mark.parametrize("field,value", [("comparison", {}), ("variants", []), ("format", True)])
def test_later_comparison_and_invalid_plan_contracts_are_refused(tmp_path, field, value):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    with pytest.raises(BenchmarkManifestError):
        load_run_options(plan_file(tmp_path, root, **{field: value}))


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--model", "override"),
        ("--repetitions", "2"),
        ("--model-retry-after-cap-seconds", "5"),
        ("--model-timeout-seconds", "300"),
    ],
)
def test_plan_rejects_explicit_overrides(tmp_path, flag, value):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = plan_file(tmp_path, root)
    args = build_parser().parse_args(
        ["eval", str(root), "--output", str(tmp_path / "out"), "--run-plan", str(path), flag, value]
    )
    with pytest.raises(CliConfigurationError, match="evaluation-run-plan-conflict"):
        _configure_from_environment(args, environment={})


async def test_run_plan_enters_real_cli_runner_and_writes_an_expired_trial(tmp_path, capsys):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = plan_file(
        tmp_path,
        root,
        execution={
            "sandbox_config": None,
            "max_trials": 1,
            "timeout_seconds": 1e-9,
        },
    )
    empty = tmp_path / "empty.env"
    empty.write_text("")
    args = build_parser().parse_args(
        [
            "eval",
            str(root),
            "--output",
            str(tmp_path / "out"),
            "--run-plan",
            str(path),
            "--env-file",
            str(empty),
        ]
    )
    _configure_from_environment(args, environment={})
    assert await _eval(args) == 4
    summary = json.loads(capsys.readouterr().out)
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert summary["run_id"] == report["run_id"]
    assert report["trials"][0]["execution"]["status"] == "not_started"
    assert summary["attempts_run"] == 0
    assert report["task_report"]["provider_id"] == "scripted"
    assert report["task_report"]["model_id"] == "fixture-model"
    assert not (tmp_path / "out/attempts").exists()


@pytest.mark.parametrize("suite", ["product_v1", "retrieval_v1"])
def test_shipped_plans_pin_the_actual_manifest_bytes(suite):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "benchmarks" / suite
    options = load_run_options(root / "run-plan.example.json")
    assert options.document.data["benchmark_digest"] == digest_bytes(
        (root / "benchmark.json").read_bytes()
    )


@pytest.mark.parametrize("relative", ["../outside.json", "C:/outside.json"])
def test_dataset_cannot_escape_its_benchmark(tmp_path, relative):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = root / "benchmark.json"
    data = json.loads(path.read_text())
    data["dataset"]["file"] = relative
    path.write_text(json.dumps(data))
    with pytest.raises(BenchmarkManifestError):
        load_benchmark_manifest(root)


def test_output_cannot_write_into_frozen_benchmark_material(tmp_path):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    output = root / "write_expected_file/initial/evidence"
    with pytest.raises(BenchmarkManifestError) as caught:
        EvaluationRunner(root, output, provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID)
    assert caught.value.code == "evaluation-output-overlap"
    assert not output.exists()


def test_missing_plan_directory_is_a_stable_configuration_error(tmp_path):
    with pytest.raises(BenchmarkManifestError) as caught:
        load_run_options(tmp_path / "absent/plan.json")
    assert caught.value.code == "evaluation-manifest-invalid"


def test_programmatic_retry_binding_matches_the_entire_plan_policy(tmp_path):
    from traceh.llm.retry import ModelRetryPolicy
    from traceh.llm.scripted import ScriptedLlmProvider

    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = plan_file(tmp_path, root)
    data = json.loads(path.read_text())
    data["model"]["retry_policy"].update(
        max_attempts=2,
        max_elapsed_seconds=10,
        max_delay_seconds=1,
    )
    path.write_text(json.dumps(data))
    policy = ModelRetryPolicy(**data["model"]["retry_policy"], retryable_categories=frozenset())
    with pytest.raises(BenchmarkManifestError) as caught:
        EvaluationRunner(
            root,
            tmp_path / "out",
            provider=ScriptedLlmProvider(()),
            model_id="fixture-model",
            retry_policy=policy,
            options=load_run_options(path),
        )
    assert caught.value.code == "evaluation-run-plan-conflict"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("suite", ["product_v1", "retrieval_v1"])
def test_git_does_not_rewrite_shipped_frozen_bytes(suite):
    import subprocess
    from pathlib import Path

    repository = Path(__file__).resolve().parents[1]
    root = repository / "benchmarks" / suite
    manifest = load_benchmark_manifest(root)
    paths = [root / "benchmark.json", root / "dataset.json", root / "run-plan.example.json"]
    for case in manifest.dataset.data["cases"]:
        paths.extend(path for path in (root / case["initial_tree"]).rglob("*") if path.is_file())
    for path in set(paths):
        relative = path.relative_to(repository).as_posix()
        raw = subprocess.run(
            ["git", "hash-object", "--no-filters", relative],
            cwd=repository,
            capture_output=True,
            check=True,
        ).stdout
        stored = subprocess.run(
            ["git", "hash-object", f"--path={relative}", relative],
            cwd=repository,
            capture_output=True,
            check=True,
        ).stdout
        assert stored == raw, relative


def _network_plan(tmp_path, root, **model):
    path = plan_file(tmp_path, root)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model"].update(
        provider="openai-compatible",
        model="network-fixture",
        base_url="https://unused.invalid/v1",
        timeout_seconds=300,
    )
    data["model"].update(model)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["absent", 0, -1, None, "300", float("inf"), True])
def test_a_plan_must_freeze_a_positive_request_timeout(tmp_path, value):
    """The request wait decides whether a long answer arrives; it is never implied."""

    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = _network_plan(tmp_path, root)
    data = json.loads(path.read_text(encoding="utf-8"))
    if value == "absent":
        del data["model"]["timeout_seconds"]
    else:
        data["model"]["timeout_seconds"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BenchmarkManifestError) as raised:
        load_run_options(path)
    assert raised.value.code == "evaluation-manifest-invalid"


def test_the_frozen_request_timeout_reaches_the_provider_and_ignores_the_environment(tmp_path):
    """A worker inherits its parent's environment; the plan, not that, sets the wait.

    Real runs cut off every answer longer than about 6,600 tokens at a 120s
    wait nobody had written down, and read the result as a hung provider.
    """

    from traceh.cli.main import _provider_and_model

    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = _network_plan(tmp_path, root)
    args = build_parser().parse_args(
        ["eval", str(root), "--output", str(tmp_path / "out"), "--run-plan", str(path)]
    )
    _configure_from_environment(args, environment={"TRACEH_MODEL_TIMEOUT_SECONDS": "7"})
    assert _provider_and_model(args)[0].timeout_seconds == 300


def test_a_provider_waiting_other_than_the_plan_is_refused(tmp_path):
    from traceh.llm.openai_compatible import OpenAICompatibleProvider

    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = _network_plan(tmp_path, root)
    provider = OpenAICompatibleProvider(
        "https://unused.invalid/v1", api_key_env="UNUSED_TEST_KEY", timeout_seconds=120
    )
    with pytest.raises(BenchmarkManifestError) as raised:
        EvaluationRunner(
            root,
            tmp_path / "out",
            provider=provider,
            model_id="network-fixture",
            options=load_run_options(path),
        )
    assert raised.value.code == "evaluation-run-plan-conflict"


async def test_the_request_timeout_is_frozen_with_the_run(tmp_path):
    from traceh.llm.openai_compatible import OpenAICompatibleProvider

    root = build_benchmark(tmp_path / "b", arms=(("single", 1),))
    path = _network_plan(
        tmp_path,
        root,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    # Expires before any request, so nothing is sent to the unused endpoint.
    data["execution"] = {"sandbox_config": None, "max_trials": 1, "timeout_seconds": 1e-9}
    path.write_text(json.dumps(data), encoding="utf-8")
    provider = OpenAICompatibleProvider(
        "https://unused.invalid/v1", api_key_env="UNUSED_TEST_KEY", timeout_seconds=300
    )
    runner = EvaluationRunner(
        root,
        tmp_path / "out",
        provider=provider,
        model_id="network-fixture",
        options=load_run_options(path),
    )
    await runner.run()
    frozen = json.loads((tmp_path / "out" / "frozen.json").read_text(encoding="utf-8"))
    assert frozen["model"]["request_timeout_seconds"] == 300
