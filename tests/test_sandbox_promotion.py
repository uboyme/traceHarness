"""Fixed verification uses real Git, Docker and the original artifact evidence."""

import asyncio
import hashlib
import tempfile
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from promotion_fixtures import (
    build_source_repository,
    make_bare_target,
    make_patch,
    promotion_targets,
    record_artifact,
)
from test_sandbox_docker import settings as settings

from traceh.api.json_types import canonical_json
from traceh.api.promotion import VerificationPlan, VerifierCommand, VerifierEnvironmentPolicy
from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.reader import PatchArtifactReader
from traceh.promotion import (
    PatchPromotionService,
    PromotionApprovalError,
    PromotionStateError,
    expected_approval_digest,
)
from traceh.promotion.models import PROMOTION_PROTOCOL_VERSION
from traceh.promotion.verification import HostVerificationRunner, VerificationEvidence
from traceh.sandbox.docker import SandboxBackendError
from traceh.sandbox.reader import read_execution
from traceh.sandbox.service import SandboxExecutionService
from traceh.session.event_store import InMemoryEventStore


def plan(code):
    return VerificationPlan(
        "sandbox-fixed-check",
        1,
        (VerifierCommand("check", ("python", "-c", code), 5000),),
        VerifierEnvironmentPolicy("explicit-guest", (), ()),
        4096,
        PROMOTION_PROTOCOL_VERSION,
    )


async def test_fixed_runner_without_configuration_never_executes_on_host(tmp_path):
    evidence = await HostVerificationRunner().run(
        plan("from pathlib import Path; Path('host-escape').touch()"),
        cwd=tmp_path,
        owner=SandboxOwner("promotion", "review-test"),
        stream_id="test-effects",
    )
    assert not evidence.passed
    assert evidence.results[0].status == "start-failed"
    assert not (tmp_path / "host-escape").exists()


@pytest.mark.parametrize(
    "code,status",
    [
        ("import sys; sys.stdout.buffer.write(bytes([255,0,1])); raise SystemExit(3)", "failed"),
        ("import time; time.sleep(60)", "timed-out"),
        ("import os; exec('while True: os.write(1,b\"x\"*65536)')", "output-exceeded"),
        ("import os; exec('while True: os.write(2,b\"x\"*65536)')", "output-exceeded"),
    ],
)
async def test_fixed_runner_failure_evidence_and_raw_output_privacy(
    tmp_path, settings, code, status
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    service = SandboxExecutionService(store=store, cas=cas, policy=settings)
    frozen = plan(code)
    if status == "timed-out":
        frozen = replace(frozen, commands=(replace(frozen.commands[0], timeout_ms=500),))
    evidence = await HostVerificationRunner(service).run(
        frozen,
        cwd=workspace,
        owner=SandboxOwner("promotion", "review-test"),
        stream_id="test-effects",
    )
    outcome = evidence.results[0]
    assert outcome.status == status
    reference = outcome.execution
    view = await read_execution(
        store, cas, stream_id=reference.stream_id, execution_id=reference.execution_id
    )
    assert view.outcome["converged"] is True
    assert view.request["export_workspace"] is False
    assert view.result["payload"]["stdout"] == ""
    if status == "failed":
        assert outcome.stdout_sha256 == hashlib.sha256(bytes([255, 0, 1])).hexdigest()
        assert outcome.stdout_bytes == 3
    if status == "output-exceeded":
        assert max(outcome.stdout_bytes, outcome.stderr_bytes) == settings.limits.output_bytes
        assert outcome.stdout_bytes <= settings.limits.output_bytes
        assert outcome.stderr_bytes <= settings.limits.output_bytes


async def test_real_patch_review_binds_receipt_and_rejects_policy_change(tmp_path, settings):
    source, _ = build_source_repository(tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    patch = make_patch(source, scratch, {"added.txt": "added\n"})
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    artifact = await record_artifact(store, cas, patch)
    reader = PatchArtifactReader(store, cas)
    resolver = promotion_targets("main", target)
    frozen = plan(
        "from pathlib import Path; assert Path('added.txt').read_text()=='added\\n'; "
        "Path('verification-only').touch(); print('fake-sensitive-verifier-output')"
    )
    sandbox = SandboxExecutionService(store=store, cas=cas, policy=settings)
    service = PatchPromotionService(store, reader, resolver, plan=frozen, sandbox_service=sandbox)
    try:
        report = await service.review(
            review_request_id="sandbox-review",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main",
        )
        assert report.passed
        reference = report.results[0].execution
        view = await read_execution(
            store, cas, stream_id=reference.stream_id, execution_id=reference.execution_id
        )
        assert view.request["owner"]["owner_id"] == report.review_id
        assert "fake-sensitive-verifier-output" not in canonical_json(view)
        assert view.publication is None

        class StaleExecutionRunner:
            async def run(self, plan, *, cwd, owner, stream_id):
                return VerificationEvidence(
                    report.verifier_definition_digest,
                    report.verification_evidence_digest,
                    report.results,
                    report.passed,
                )

        wrong_owner = PatchPromotionService(
            store,
            reader,
            resolver,
            plan=frozen,
            sandbox_service=sandbox,
            runner=StaleExecutionRunner(),
        )
        try:
            with pytest.raises(PromotionStateError) as rejected_owner:
                await wrong_owner.review(
                    review_request_id="another-review",
                    artifact_id=artifact.manifest.artifact_id,
                    target_id="main",
                )
            assert rejected_owner.value.code == "promotion-sandbox-reference-mismatch"
        finally:
            await wrong_owner.aclose()
        assert (
            await service.review(
                review_request_id="sandbox-review",
                artifact_id=artifact.manifest.artifact_id,
                target_id="main",
            )
            == report
        )
        changed = SandboxExecutionService(
            store=store,
            cas=cas,
            policy=replace(settings, limits=replace(settings.limits, cpus=0.25)),
        )
        other = PatchPromotionService(store, reader, resolver, plan=frozen, sandbox_service=changed)
        try:
            with pytest.raises(PromotionApprovalError) as rejected:
                await other.approve(
                    review_id=report.review_id,
                    approval_digest=expected_approval_digest(report),
                    approver_id="human",
                    operation_id="wrong-policy",
                )
            assert rejected.value.code == "promotion-review-verification-mismatch"
        finally:
            await other.aclose()
    finally:
        await service.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_fixed_verifier_preserves_work_and_cleanup_failure_after_cancellation(
    tmp_path,
    settings,
    monkeypatch,
    cancel,
):
    import traceh.sandbox.docker as docker

    entered = threading.Event()
    release = threading.Event()
    resources = []

    class RefusingCleanup:
        def __init__(self, **kwargs):
            self.real = tempfile.TemporaryDirectory(dir=tmp_path, **kwargs)
            self.name = self.real.name
            resources.append(self)

        def cleanup(self):
            entered.set()
            if not release.wait(30):
                raise RuntimeError("test cleanup gate was not released")
            raise OSError("synthetic control cleanup failure")

    monkeypatch.setattr(
        docker,
        "tempfile",
        SimpleNamespace(
            TemporaryDirectory=RefusingCleanup,
            TemporaryFile=tempfile.TemporaryFile,
        ),
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    service = SandboxExecutionService(store=store, cas=cas, policy=settings)
    running = asyncio.create_task(
        HostVerificationRunner(service).run(
            plan("print('executed-before-cleanup'); raise SystemExit(3)"),
            cwd=workspace,
            owner=SandboxOwner("promotion", "cleanup-review"),
            stream_id="test-effects",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 30), "real cleanup was not reached"
        if cancel:
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else SandboxBackendError) as rejected:
            await running
        if cancel:
            assert isinstance(rejected.value.__cause__, SandboxBackendError)
        events = await store.read("test-effects")
        receipt = events[-1].data
        assert events[-1].type == "sandbox/outcome"
        assert receipt["converged"] is True
        assert receipt["cleanup_failures"] == ["sandbox-control-cleanup-failed"]
        view = await read_execution(
            store, cas, stream_id="test-effects", execution_id=receipt["execution_id"]
        )
        assert view.result["payload"]["exit_code"] == 3
        assert (
            view.result["payload"]["stdout_sha256"]
            == hashlib.sha256(b"executed-before-cleanup\n").hexdigest()
        )
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        for resource in resources:
            from pathlib import Path

            assert Path(resource.name).parent == tmp_path
            resource.real.cleanup()
