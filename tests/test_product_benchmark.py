"""The F4 manifest, its refusal of the v0.6 layout, and honest aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceh.api.product import (
    ProductRole,
    ProductTaskStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.workflow import WorkflowStatus
from traceh.api.workspaces import WorkspaceAccess
from traceh.cli.main import build_parser
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.manifest import (
    BENCHMARK_SOURCE_ID,
    BENCHMARK_SOURCE_REVISION,
    BENCHMARK_TARGET_ID,
    LEGACY_CASE_FILENAME,
    MANIFEST_FILENAME,
    load_benchmark_manifest,
)
from traceh.evaluation.metrics import (
    AttemptEvidence,
    BudgetOutcome,
    SessionGroup,
    SessionWork,
    TokenTotals,
    WorkspaceOutcome,
)
from traceh.evaluation.report import (
    APPROVAL_POLICY,
    AttemptReport,
    BenchmarkReport,
    PhaseTiming,
    build_task_conditions,
    render_markdown,
    summarize,
)
from traceh.product.config import PRODUCT_HOST_SETTINGS_KEYS
from traceh.product.registry import ProductProfileBinding, ProductProfileRegistry

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_BENCHMARK = REPOSITORY_ROOT / "benchmarks" / "product_v1"


def _write(root: Path, manifest: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task-a" / "initial").mkdir(parents=True, exist_ok=True)
    (root / "task-a" / "initial" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _manifest() -> dict[str, object]:
    raw = json.loads(
        (SHIPPED_BENCHMARK / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    raw["tasks"] = [
        {
            "task_id": "task-a",
            "requirement": "Do the one thing the frozen checks require.",
            "initial_dir": "task-a/initial",
        }
    ]
    raw["arms"] = [{"requested_mode": "single", "repetitions": 1}]
    return raw


def _load(root: Path):
    return load_benchmark_manifest(
        root, provider_id="a-provider", model_id="a-model"
    )


# ------------------------------------------------------------------- manifest


def test_the_shipped_benchmark_is_a_valid_schema_1_manifest() -> None:
    manifest = _load(SHIPPED_BENCHMARK)

    assert manifest.benchmark_id == "traceh-product-v1"
    assert len(manifest.tasks) == 3
    assert {arm.requested_mode for arm in manifest.arms} == set(RequestedTaskMode)
    assert manifest.attempt_count == 3 * sum(
        arm.repetitions for arm in manifest.arms
    )
    profile = manifest.settings.host_profile.profile
    # Provider, model, source and target are bindings the runner supplies. A
    # manifest that could name them could point this command somewhere real.
    assert profile.provider_id == "a-provider"
    assert profile.model_id == "a-model"
    assert profile.source_id == BENCHMARK_SOURCE_ID
    assert profile.source_revision == BENCHMARK_SOURCE_REVISION
    assert profile.promotion_target_id == BENCHMARK_TARGET_ID


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for item in value.values():
            result |= _keys(item)
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result |= _keys(item)
        return result
    return set()


def test_the_shipped_manifest_cannot_name_a_repository_or_a_graph() -> None:
    raw = json.loads(
        (SHIPPED_BENCHMARK / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )

    # Checked over the key set at every depth: a requirement is prose and may
    # legitimately contain the word "repository", but no key may.
    assert _keys(raw).isdisjoint(
        {
            "repository",
            "promotion_target",
            "managed_workspace_root",
            "cas_root",
            "provider_id",
            "model_id",
            "nodes",
            "edges",
            "graph",
            "fan_out",
            "agents",
            "approval_digest",
        }
    )
    assert set(raw) == PRODUCT_HOST_SETTINGS_KEYS | {
        "protocol_version",
        "benchmark_id",
        "arms",
        "tasks",
    }


async def test_the_shipped_profile_resolves_against_the_real_registry() -> None:
    """The manifest is not merely well-shaped; a host can actually run it."""

    from traceh.product.runtime import BuiltinProductAssemblyResolver

    manifest = _load(SHIPPED_BENCHMARK)
    registry = ProductProfileRegistry(
        (
            (
                manifest.settings.host_profile.profile_id,
                ProductProfileBinding(
                    profile=manifest.settings.host_profile.profile,
                    verification_plan=manifest.settings.host_profile.verification_plan,
                ),
            ),
        ),
        assemblies=BuiltinProductAssemblyResolver(),
    )
    resolved = await registry.resolve(manifest.settings.host_profile.profile_id)

    assert resolved.router.tool_ids == ()
    assert resolved.assembly(ProductRole.CODER).workspace_access is (
        WorkspaceAccess.WRITABLE
    )
    assert resolved.assembly(ProductRole.REVIEWER).workspace_access is (
        WorkspaceAccess.READ_ONLY
    )
    assert resolved.assembly(ProductRole.PARENT).workspace_access is (
        WorkspaceAccess.READ_ONLY
    )


def test_the_v06_case_json_layout_is_refused_without_being_read(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "fix_addition").mkdir(parents=True)
    (legacy / "fix_addition" / LEGACY_CASE_FILENAME).write_text(
        json.dumps({"name": "fix_addition", "task": "x", "verify_command": "y"}),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(legacy)

    assert caught.value.code == "benchmark-legacy-manifest-rejected"


def test_a_directory_without_a_manifest_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(empty)

    assert caught.value.code == "benchmark-manifest-missing"


@pytest.mark.parametrize(
    "extra", ["nodes", "edges", "agents", "provider_id", "promotion_target"]
)
def test_an_unknown_manifest_key_is_a_rejection(tmp_path: Path, extra: str) -> None:
    manifest = _manifest()
    manifest[extra] = []

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(_write(tmp_path / "b", manifest))

    assert caught.value.code == "benchmark-manifest-shape-invalid"


def test_an_unsupported_protocol_version_is_a_rejection(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["protocol_version"] = 2

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(_write(tmp_path / "b", manifest))

    assert caught.value.code == "benchmark-manifest-version-unsupported"


def test_two_entries_for_one_mode_cannot_become_one_silent_arm(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest["arms"] = [
        {"requested_mode": "single", "repetitions": 1},
        {"requested_mode": "single", "repetitions": 3},
    ]

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(_write(tmp_path / "b", manifest))

    assert caught.value.code == "benchmark-manifest-arm-duplicate"


@pytest.mark.parametrize("path", ["../outside", "/absolute/outside", "missing/dir"])
def test_a_task_tree_outside_the_benchmark_is_refused(
    tmp_path: Path, path: str
) -> None:
    manifest = _manifest()
    tasks = manifest["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["initial_dir"] = path

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(_write(tmp_path / "b", manifest))

    assert caught.value.code in {
        "benchmark-manifest-path-invalid",
        "benchmark-manifest-path-missing",
    }


def test_a_profile_the_product_domain_refuses_is_refused_here(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    roles = manifest["roles"]
    assert isinstance(roles, dict)
    roles["coder"]["budget"]["max_tokens"] = -1

    with pytest.raises(BenchmarkManifestError) as caught:
        _load(_write(tmp_path / "b", manifest))

    # The Product domain's own rule decided this, not a weaker benchmark copy.
    assert caught.value.code.startswith("product-host-config-")


# --------------------------------------------------------------- aggregation


def _evidence(
    *,
    resolved: ResolvedTaskMode,
    tokens: int | None = 100,
    success: bool = True,
    profile_digest: str = "profile-1",
    source_base_revision: str = "rev-1",
    duration_ms: int | None = 900,
) -> AttemptEvidence:
    session = SessionWork(
        session_id=f"session-{resolved.value}",
        agent_id=f"agent-{resolved.value}",
        turns=1,
        steps=2,
        tool_calls=1,
        tokens=None if tokens is None else TokenTotals(tokens, 0, "exact"),
        work_duration_ms=duration_ms,
    )
    return AttemptEvidence(
        task_id="product-task-1",
        product_status=(
            ProductTaskStatus.COMPLETED if success else ProductTaskStatus.FAILED
        ),
        requested_mode=RequestedTaskMode.SINGLE,
        mode_source=TaskModeSource.CONFIRMED_PROPOSAL,
        resolved_mode=resolved,
        requirement_digest="requirement-1",
        profile_digest=profile_digest,
        preflight_digest="preflight-1",
        source_base_revision=source_base_revision,
        definition_hash="definition-1",
        workflow_status=(
            WorkflowStatus.COMPLETED if success else WorkflowStatus.FAILED
        ),
        routing=None,
        routing_parsed=False,
        execution=SessionGroup((session,)),
        unattributed=SessionGroup(()),
        budget=BudgetOutcome(2, 2, 1, 1, 3, 3, 90, 2, 1),
        workspaces=WorkspaceOutcome(2, 2, 0),
        review_id="review-1" if success else None,
        review_passed=True if success else None,
        verifier_definition_digest="verifier-1" if success else None,
        promotion_id="promotion-1" if success else None,
        previous_revision="rev-1" if success else None,
        new_revision="rev-2" if success else None,
        target_ref="refs/heads/main",
        target_revision="rev-2" if success else None,
        failure_code=None if success else "workflow-failed",
        reason_code=None,
        unavailable=() if tokens is not None else ("execution.tokens",),
    )


def _attempt(
    evidence: AttemptEvidence | None,
    *,
    requested: RequestedTaskMode,
    repetition: int = 1,
    wall_ms: int = 1000,
    approval_wait_ms: int = 200,
) -> AttemptReport:
    return AttemptReport(
        attempt_id=f"task-a/{requested.value}/{repetition}",
        benchmark_task_id="task-a",
        requested_mode=requested,
        repetition=repetition,
        directory=f"attempts/{repetition:03d}",
        error_code=None if evidence is not None else "benchmark-attempt-failed",
        evidence=evidence,
        timing=PhaseTiming(wall_ms=wall_ms, approval_wait_ms=approval_wait_ms),
    )


FROZEN_VERIFIER_DIGEST = "verifier-1"


def _report(attempts: tuple[AttemptReport, ...]) -> BenchmarkReport:
    return BenchmarkReport(
        benchmark_id="b",
        protocol_version=1,
        profile_id="p",
        provider_id="a-provider",
        model_id="a-model",
        attempts=attempts,
        tasks=(
            build_task_conditions(
                "task-a",
                attempts,
                verifier_definition_digest=FROZEN_VERIFIER_DIGEST,
            ),
        ),
    )


def test_an_unavailable_measurement_is_counted_not_zeroed() -> None:
    summary = summarize([10, None, 30])

    assert summary.observations == 2
    assert summary.unavailable == 1
    assert summary.total == 40
    assert summary.mean == 20.0
    # A zero substituted for the missing value would have produced 13.33.


def test_auto_never_becomes_a_third_quality_arm() -> None:
    report = _report(
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.MULTI),
                requested=RequestedTaskMode.MULTI,
            ),
            _attempt(
                _evidence(resolved=ResolvedTaskMode.MULTI),
                requested=RequestedTaskMode.AUTO,
                repetition=2,
            ),
        )
    )
    data = report.to_dict()

    assert [arm["resolved_mode"] for arm in data["quality_arms"]] == ["multi"]
    assert data["quality_arms"][0]["observations"] == 2
    assert data["quality_arms"][0]["requested_modes"] == {"auto": 1, "multi": 1}
    assert data["routing_arm"]["observations"] == 1
    assert data["routing_arm"]["resolved_modes"] == {"multi": 1}


def test_one_observation_is_labelled_and_two_are_not() -> None:
    single = _report(
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE),
                requested=RequestedTaskMode.SINGLE,
            ),
        )
    )
    repeated = _report(
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE),
                requested=RequestedTaskMode.SINGLE,
            ),
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE, tokens=140),
                requested=RequestedTaskMode.SINGLE,
                repetition=2,
            ),
        )
    )

    assert single.quality_arms[0].single_observation is True
    assert "single observation" in render_markdown(single)
    assert repeated.quality_arms[0].single_observation is False
    assert "single observation" not in render_markdown(repeated)
    arm = repeated.quality_arms[0].to_dict()
    assert arm["execution_tokens"]["values"] == [100, 140]
    assert arm["execution_tokens"]["total"] == 240
    assert arm["execution_tokens"]["mean"] == 120.0


def test_an_arm_that_never_reached_a_review_leaves_the_verifier_unproven() -> None:
    """A condition only one arm demonstrated is not a shared condition.

    The frozen manifest digest is the authority, so the value is still exact;
    what changes is that the report says which arm never proved it rather than
    filtering the absence away into apparent agreement.
    """

    succeeded = _evidence(resolved=ResolvedTaskMode.SINGLE)
    failed_before_review = _evidence(resolved=ResolvedTaskMode.MULTI, success=False)
    assert failed_before_review.verifier_definition_digest is None
    conditions = build_task_conditions(
        "task-a",
        (
            _attempt(succeeded, requested=RequestedTaskMode.SINGLE),
            _attempt(
                failed_before_review, requested=RequestedTaskMode.MULTI, repetition=2
            ),
        ),
        verifier_definition_digest=FROZEN_VERIFIER_DIGEST,
    )

    assert conditions.verifier_definition_digest == FROZEN_VERIFIER_DIGEST
    assert "verifier_definition_digest" in conditions.unproven_fields
    # It did not *contradict* anything, so the experiment is still coherent -
    # but the reader is told the column was not proved everywhere.
    assert conditions.coherent
    assert conditions.divergent_fields == ()


def test_an_arm_that_used_another_verifier_is_a_divergence() -> None:
    """Agreement between arms is not enough; they must match the frozen plan."""

    conditions = build_task_conditions(
        "task-a",
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE),
                requested=RequestedTaskMode.SINGLE,
            ),
        ),
        verifier_definition_digest="a-different-frozen-plan",
    )

    assert conditions.divergent_fields == ("verifier_definition_digest",)
    assert not conditions.coherent


def test_a_quarantined_workspace_is_a_converged_terminal() -> None:
    """Quarantine is how the Product contract preserves dirty failure evidence."""

    assert WorkspaceOutcome(2, 1, 1).converged is True
    assert WorkspaceOutcome(2, 1, 1).live == 0
    assert WorkspaceOutcome(2, 2, 0).converged is True
    # A record nobody released or quarantined is what "not converged" means.
    assert WorkspaceOutcome(2, 1, 0).converged is False
    assert WorkspaceOutcome(2, 1, 0).live == 1
    assert WorkspaceOutcome(0, 0, 0).converged is False


def test_a_divergent_experiment_condition_is_named_not_averaged() -> None:
    report = _report(
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE),
                requested=RequestedTaskMode.SINGLE,
            ),
            _attempt(
                _evidence(
                    resolved=ResolvedTaskMode.MULTI,
                    profile_digest="profile-2",
                    source_base_revision="rev-9",
                ),
                requested=RequestedTaskMode.MULTI,
                repetition=2,
            ),
        )
    )

    (conditions,) = report.tasks
    assert not conditions.coherent
    assert conditions.divergent_fields == ("profile_digest", "source_base_revision")
    assert report.complete is False
    assert "profile_digest, source_base_revision" in render_markdown(report)


def test_an_unmeasured_attempt_keeps_the_run_incomplete() -> None:
    report = _report(
        (
            _attempt(None, requested=RequestedTaskMode.SINGLE),
        )
    )

    assert report.measured == 0
    assert report.complete is False
    assert "attempt error benchmark-attempt-failed" in render_markdown(report)


def test_the_markdown_reads_the_same_dictionary_the_json_does() -> None:
    report = _report(
        (
            _attempt(
                _evidence(resolved=ResolvedTaskMode.SINGLE, tokens=None),
                requested=RequestedTaskMode.SINGLE,
            ),
        )
    )
    data = report.to_dict()
    markdown = render_markdown(report)

    assert data["approval_policy"] == APPROVAL_POLICY
    assert APPROVAL_POLICY in markdown
    assert "execution.tokens" in markdown
    assert "unavailable=1" in markdown
    assert str(data["attempts"][0]["timing"]["active_ms"]) in markdown
    assert data["attempts"][0]["timing"]["active_ms"] == 800


# --------------------------------------------------------------------- the CLI


def test_the_eval_command_takes_one_manifest_and_a_new_output_directory() -> None:
    args = build_parser().parse_args(
        ["eval", "benchmarks/product_v1", "--output", "run-1", "--model", "m"]
    )

    assert args.benchmark == Path("benchmarks/product_v1")
    assert args.output == Path("run-1")
    assert args.model == "m"
    # A benchmark owns its own data directories, verifier and plugins, so those
    # arguments are deliberately absent rather than accepted and ignored.
    for absent in ("data_dir", "verify_command", "verifier_name", "plugins", "max_steps"):
        assert not hasattr(args, absent), absent


def test_the_eval_command_requires_an_output_directory() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval", "benchmarks/product_v1"])
