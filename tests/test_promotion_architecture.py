"""D2 dependency, authority and future-stage boundary guards."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import get_type_hints

import pytest

import traceh.artifacts.capture as artifact_capture_module
import traceh.artifacts.reporting as artifact_reporting_module
import traceh.plugins.manager as plugin_manager_module
import traceh.promotion.service as promotion_service_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workspaces.supervision as workspace_supervision_module
from traceh.api import promotion as promotion_api
from traceh.api.promotion import PromotionTargetResolver, VerificationPlan
from traceh.api.tools import EffectKind
from traceh.artifacts.reader import PatchArtifactReader
from traceh.promotion import PatchPromotionService
from traceh.supervision.tools import CollectAgentArtifactTool

PROMOTION_ROOT = Path(promotion_service_module.__file__).parent
EXECUTION_OWNERS = (
    agent_loop_module,
    agent_runtime_module,
    supervisor_module,
    plugin_manager_module,
)


def _sources(root: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(
        (source, source.read_text(encoding="utf-8"))
        for source in sorted(root.glob("*.py"))
    )


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def _strings(text: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_promotion_uses_narrow_public_seams() -> None:
    hints = get_type_hints(PatchPromotionService.__init__)
    assert hints["artifacts"] is PatchArtifactReader
    assert hints["resolver"] is PromotionTargetResolver
    assert hints["plan"] is VerificationPlan
    for name in (
        "PatchApproval",
        "PatchPromotion",
        "PatchReviewReport",
        "PromotionTarget",
        "PromotionTargetBinding",
        "VerificationPlan",
        "VerifierCommand",
        "VerifierEnvironmentPolicy",
        "VerifierOutcome",
    ):
        value = getattr(promotion_api, name)
        assert dataclasses.is_dataclass(value), name
        assert value.__dataclass_params__.frozen, name
        assert getattr(value, "__slots__", None) is not None, name


def test_promotion_never_imports_the_execution_or_plugin_owners() -> None:
    forbidden = {
        "traceh.runtime.agent_loop",
        "traceh.runtime.agent_runtime",
        "traceh.runtime.composition_runtime",
        "traceh.supervision.supervisor",
        "traceh.plugins.manager",
        "traceh.kernel.activation",
        "traceh.evolution",
    }
    for source, text in _sources(PROMOTION_ROOT):
        imported = {
            node.module
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert forbidden.isdisjoint(imported), source.name
        assert not any(name.startswith("traceh.cli") for name in imported), source.name


def test_no_existing_owner_learns_about_promotion() -> None:
    modules = (
        *EXECUTION_OWNERS,
        tools_module,
        workspace_supervision_module,
        artifact_capture_module,
        artifact_reporting_module,
    )
    for module in modules:
        assert not any(
            name == "traceh.promotion" or name.startswith("traceh.promotion.")
            for name in _imports(module)
        ), module.__name__


def test_no_standalone_patch_promotion_cli_exists() -> None:
    package = Path(agent_runtime_module.__file__).parent.parent
    for source, text in _sources(package / "cli"):
        # F3's explicit composition root may select a local target resolver and
        # translate Promotion errors. It still must not own the service, ledger,
        # stream, or a parallel Patch-promotion subcommand.
        assert "PatchPromotionService" not in text, source.name
        assert "patch-promotions" not in text, source.name
    assert not (package / "workflows").exists()
    assert not (package / "promotion" / "cli.py").exists()


PRODUCT_PROMOTION_IMPORTS = {
    "assembly.py": {
        "traceh.promotion.models": {"require_target_ref"},
    },
    "control.py": {
        "traceh.promotion.models": {"expected_approval_digest"},
        "traceh.promotion.service": {"PatchPromotionService"},
    },
    "events.py": {
        "traceh.promotion.models": {"require_target_ref"},
    },
    "inspection.py": {
        "traceh.promotion.models": {"review_matches_verification_plan"},
    },
    "observation.py": {
        "traceh.promotion.events": {"PROMOTION_LEDGER_STREAM"},
        "traceh.promotion.models": {"expected_approval_digest"},
        "traceh.promotion.projection": {"PromotionLedgerReader"},
    },
    "host.py": {
        "traceh.promotion.service": {"PatchPromotionService"},
    },
    "registry.py": {
        "traceh.promotion.models": {
            "freeze_verification_plan",
            "verifier_definition_digest",
        },
    },
}
"""The exact F2/F3 Product-to-Promotion dependency surface.

F2 reuses Promotion's identity rules instead of copying them. v0.7 F3 adds the
host and control-plane orchestration consumers; v0.8 F3 adds one pure ledger
reader for UI observation. Per-file symbol sets prevent those seams from
becoming blanket domain access.
"""


CLI_PROMOTION_IMPORTS = {
    "main.py": {
        "traceh.promotion.errors": {"PromotionError"},
        "traceh.promotion.local_git": {"LocalBareGitPromotionTargets"},
    },
}


EVALUATION_PROMOTION_IMPORTS = {
    "attempt.py": {
        "traceh.promotion.local_git": {"LocalBareGitPromotionTargets"},
    },
    "manifest.py": {
        "traceh.promotion.models": {"verifier_definition_digest"},
    },
    "metrics.py": {
        "traceh.promotion.models": {
            "expected_approval_digest",
            "review_matches_verification_plan",
        },
        "traceh.promotion.projection": {"PromotionLedgerReader"},
    },
}
"""F4's benchmark host is a composition root, and a reader of its own evidence.

It selects the concrete one-shot bare target for each attempt, exactly as
``cli/main.py`` selects one for Chat, and it reads the ledger back to decide
whether a promotion really happened. Promotion's approval digest and shared
frozen-plan Review matcher are reused rather than reimplemented, for the same
reason F2 reuses its identity rules: a benchmark that computed its own answer to
"which verifier" or "which approval covers this Review" would be a second answer
that drifts. It owns no Review, Approval or Promotion *operation*: the whole run
goes through the Product control plane, and the symbol sets here are what keeps
that true as the stage advances.
"""


def test_only_declared_orchestration_seams_import_the_promotion_domain() -> None:
    """Promotion remains behind Workflow, Product and two composition roots.

    Runtime/Agent/Tool owners still know nothing about Promotion. Product is an
    F3 orchestration layer, while ``cli/main.py`` and F4's ``evaluation/`` only
    choose the concrete target resolver, read the resulting ledger and translate
    stable errors at their explicit assembly boundaries. Every permitted concrete
    import is pinned by file and symbol; all other modules must have an empty
    dependency set.
    """

    package = Path(agent_runtime_module.__file__).parent.parent
    allowed = {PROMOTION_ROOT, package / "workflow"}
    for source in sorted(package.rglob("*.py")):
        if source.parent in allowed:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = {
            node.module: {alias.name for alias in node.names}
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
            and (
                node.module == "traceh.promotion"
                or node.module.startswith("traceh.promotion.")
            )
        }
        if source.parent == package / "product":
            assert imports == PRODUCT_PROMOTION_IMPORTS.get(source.name, {}), (
                source.name,
                imports,
            )
            continue
        if source.parent == package / "cli":
            assert imports == CLI_PROMOTION_IMPORTS.get(source.name, {}), (
                source.name,
                imports,
            )
            continue
        if source.parent == package / "evaluation":
            assert imports == EVALUATION_PROMOTION_IMPORTS.get(source.name, {}), (
                source.name,
                imports,
            )
            continue
        assert not imports, str(source.relative_to(package))


def test_the_model_gains_no_approve_merge_or_promote_tool() -> None:
    toolset_source = Path(tools_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "approve",
        "promote",
        "promotion",
        "update-ref",
        "update_ref",
        "merge",
        "PatchPromotionService",
        "PatchReviewReport",
    ):
        assert forbidden not in toolset_source, forbidden
    effects = {
        field.name: field.default
        for field in dataclasses.fields(CollectAgentArtifactTool)
    }
    assert effects["effect_kind"] is EffectKind.PURE_READ
    assert effects["name"] == "collect_agent_artifact"
    assert set(tools_module.__all__) == {
        "AgentToolAuthority",
        "AgentToolAuthorizationError",
        "AgentToolBindingError",
        "CollectAgentArtifactTool",
        "SendAgentMessageTool",
        "SpawnAgentTool",
        "StopAgentTool",
        "SupervisorToolset",
        "WaitAgentTool",
    }


def test_promotion_never_runs_a_shell_or_a_second_scheduler() -> None:
    for source, text in _sources(PROMOTION_ROOT):
        assert "shell=True" not in text, source.name
        assert "create_subprocess_shell" not in text, source.name
        tree = ast.parse(text)
        classes = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        ]
        assert not any(
            name.endswith("Supervisor") or name.endswith("Activation")
            for name in classes
        ), source.name


def test_no_test_fixture_identity_leaks_into_production_code() -> None:
    forbidden = (
        "main-target",
        "trusted-source",
        "coder-agent",
        "work-message",
        "review-request",
        "release-manager",
        "fixture",
        "pytest",
        "tmp_path",
        "example.invalid",
    )
    for source, text in _sources(PROMOTION_ROOT):
        literals = _strings(text)
        for value in forbidden:
            assert not any(value in literal for literal in literals), (
                source.name,
                value,
            )


def test_the_promotion_ledger_is_the_only_new_durable_stream() -> None:
    streams: set[str] = set()
    for _, text in _sources(PROMOTION_ROOT):
        streams.update(
            literal for literal in _strings(text) if literal.count(":") == 1
            and literal.split(":")[0] in {"patch-promotions", "session", "agents"}
        )
    assert streams == {"patch-promotions:ledger"}


async def test_the_output_bound_is_enforced_while_a_verifier_is_running() -> None:
    import sys
    import tempfile

    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner

    plan = VerificationPlan(
        plan_id="flooding-verifier",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="floods-stdout",
                argv=(
                    sys.executable,
                    "-c",
                    "import sys\n"
                    "block = b'x' * 65536\n"
                    "for _ in range(128):\n"
                    "    sys.stdout.buffer.write(block)\n"
                    "sys.stdout.buffer.flush()\n",
                ),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="flood-env",
            passthrough=(
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "TMPDIR",
            ),
            overrides=(),
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        evidence = await HostVerificationRunner().run(plan, cwd=Path(workdir))
    outcome = evidence.results[0]
    assert outcome.status == "output-exceeded"
    assert evidence.passed is False
    # The bound must stop the writer, not merely describe it afterwards, and
    # the recorded size must not depend on how fast the kill landed: at most one
    # read chunk may cross the limit, whatever the machine load.
    assert outcome.stdout_bytes <= 1024 + 65536, outcome.stdout_bytes


async def test_a_verifier_timeout_is_not_extended_by_a_surviving_grandchild() -> None:
    import sys
    import tempfile
    import time

    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner

    plan = VerificationPlan(
        plan_id="orphan-grandchild",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="spawns-and-exits",
                argv=(
                    sys.executable,
                    "-c",
                    "import subprocess, sys\n"
                    "subprocess.Popen([sys.executable, '-c',"
                    " 'import time; time.sleep(5)'])\n"
                    "raise SystemExit(0)\n",
                ),
                timeout_ms=200,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="orphan-env",
            passthrough=(
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "TMPDIR",
            ),
            overrides=(),
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        started = time.monotonic()
        evidence = await HostVerificationRunner().run(plan, cwd=Path(workdir))
        elapsed = time.monotonic() - started
    # A grandchild holding an inherited handle must not extend the bound the
    # host configured for the command it actually owns.
    assert elapsed < 3.0, elapsed
    assert evidence.results[0].status in {"passed", "timed-out"}


async def test_an_explicit_temp_override_outranks_the_granted_scratch() -> None:
    import tempfile

    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion.verification import _verifier_environment

    command = VerifierCommand(
        command_id="unused", argv=("verifier",), timeout_ms=1000
    )
    granted = Path(tempfile.gettempdir()) / "granted-scratch"

    inherited = VerificationPlan(
        plan_id="inherits-temp",
        plan_version=1,
        commands=(command,),
        environment=VerifierEnvironmentPolicy(
            policy_id="inherit", passthrough=("TEMP", "TMP", "TMPDIR"), overrides=()
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    # Passthrough only inherits an ambient value, so owned scratch outranks it.
    assert _verifier_environment(inherited, granted)["TEMP"] == str(granted)

    decided = VerificationPlan(
        plan_id="overrides-temp",
        plan_version=1,
        commands=(command,),
        environment=VerifierEnvironmentPolicy(
            policy_id="decide",
            passthrough=(),
            overrides=(("TEMP", "C:/host-chosen"),),
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    environment = _verifier_environment(decided, granted)
    assert environment["TEMP"] == "C:/host-chosen"
    assert environment["TMPDIR"] == str(granted)


class _ExplodingShutil:
    """A ``shutil`` stand-in that refuses to remove a tree.

    It reproduces the real ``ignore_errors`` semantics on purpose: a caller that
    silences the failure must be visible here, otherwise this stub would happily
    pass for an implementation that hides cleanup errors.
    """

    @staticmethod
    def rmtree(root, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
        del onerror, onexc, dir_fd
        if ignore_errors:
            return
        raise OSError(f"forced cleanup failure for {Path(root).name}")


async def test_verifier_scratch_cleanup_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import sys
    import tempfile

    import traceh.promotion.verification as verification
    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner, PromotionVerificationError

    # Rebind only this module's ``shutil`` name, so the test's own temporary
    # directory cleanup keeps working.
    monkeypatch.setattr(verification, "shutil", _ExplodingShutil())
    del shutil

    plan = VerificationPlan(
        plan_id="cleanup-boundary",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="succeeds",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="cleanup-env",
            passthrough=("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"),
            overrides=(),
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        with pytest.raises(PromotionVerificationError) as raised:
            await HostVerificationRunner().run(plan, cwd=Path(workdir))
    assert raised.value.code == "promotion-verifier-scratch-cleanup-failed"
    assert isinstance(raised.value.__cause__, OSError)


async def test_verifier_scratch_cleanup_failure_never_masks_the_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import tempfile

    import traceh.promotion.verification as verification
    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner

    monkeypatch.setattr(verification, "shutil", _ExplodingShutil())

    class _Failing(HostVerificationRunner):
        async def _execute(self, *args, **kwargs):
            raise RuntimeError("the command boundary failed first")

    plan = VerificationPlan(
        plan_id="cleanup-and-primary",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="never-runs",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="cleanup-env", passthrough=("PATH",), overrides=()
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        with pytest.raises(BaseExceptionGroup) as raised:
            await _Failing().run(plan, cwd=Path(workdir))
    messages = [str(error) for error in raised.value.exceptions]
    assert any("the command boundary failed first" in item for item in messages)
    assert any("forced cleanup failure" in item for item in messages)


class _BlockingExplodingShutil:
    """Block inside ``rmtree`` until released, then fail.

    This is what makes the cancellation race deterministic: the caller can be
    cancelled while cleanup is genuinely in flight, and the cleanup failure
    arrives strictly afterwards.
    """

    def __init__(self) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()

    def rmtree(self, root, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
        del onerror, onexc, dir_fd
        self.entered.set()
        self.release.wait(30)
        if ignore_errors:
            return
        raise OSError(f"forced cleanup failure for {Path(root).name}")


async def test_cancelling_during_cleanup_keeps_the_cleanup_failure_as_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import sys
    import tempfile

    import traceh.promotion.verification as verification
    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner

    blocking = _BlockingExplodingShutil()
    monkeypatch.setattr(verification, "shutil", blocking)

    plan = VerificationPlan(
        plan_id="cancel-during-cleanup",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="succeeds",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="cleanup-env",
            passthrough=("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"),
            overrides=(),
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        running = asyncio.create_task(
            HostVerificationRunner().run(plan, cwd=Path(workdir))
        )
        # Cleanup is genuinely in flight before the caller is cancelled.
        await asyncio.to_thread(blocking.entered.wait, 30)
        running.cancel()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        blocking.release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await running

    # The caller still sees its own cancellation, but the cleanup failure that
    # happened afterwards is retained rather than discarded.
    cause = raised.value.__cause__
    assert isinstance(cause, OSError), (cause, raised.value.__context__)
    assert "forced cleanup failure" in str(cause)


class _BlockingShutil:
    """Block inside ``rmtree`` until released, then optionally fail."""

    def __init__(self, *, fail: bool) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()
        self._fail = fail

    def rmtree(self, root, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
        del onerror, onexc, dir_fd
        self.entered.set()
        self.release.wait(30)
        if self._fail and not ignore_errors:
            raise OSError(f"forced cleanup failure for {Path(root).name}")


def _cleanup_plan(plan_id: str):
    import sys

    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )

    return VerificationPlan(
        plan_id=plan_id,
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="never-runs",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="cleanup-env", passthrough=("PATH",), overrides=()
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )


async def _cancel_during_cleanup(monkeypatch, *, fail_cleanup: bool):
    import asyncio
    import tempfile

    import traceh.promotion.verification as verification
    from traceh.promotion import HostVerificationRunner

    blocking = _BlockingShutil(fail=fail_cleanup)
    monkeypatch.setattr(verification, "shutil", blocking)

    class _Failing(HostVerificationRunner):
        async def _execute(self, *args, **kwargs):
            raise RuntimeError("verifier-failed-before-cleanup")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        running = asyncio.create_task(
            _Failing().run(_cleanup_plan("cancel-with-primary"), cwd=Path(workdir))
        )
        await asyncio.to_thread(blocking.entered.wait, 30)
        running.cancel()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        blocking.release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await running
    return raised.value


def _flatten(error):
    if isinstance(error, BaseExceptionGroup):
        found = []
        for item in error.exceptions:
            found.extend(_flatten(item))
        return found
    return [error]


async def test_cancelling_during_cleanup_keeps_an_existing_verifier_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = await _cancel_during_cleanup(monkeypatch, fail_cleanup=False)
    causes = _flatten(cancellation.__cause__)
    assert any(
        isinstance(item, RuntimeError)
        and "verifier-failed-before-cleanup" in str(item)
        for item in causes
    ), causes


async def test_cancelling_during_a_failing_cleanup_keeps_both_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = await _cancel_during_cleanup(monkeypatch, fail_cleanup=True)
    causes = _flatten(cancellation.__cause__)
    assert any(
        isinstance(item, RuntimeError)
        and "verifier-failed-before-cleanup" in str(item)
        for item in causes
    ), causes
    assert any(
        isinstance(item, OSError) and "forced cleanup failure" in str(item)
        for item in causes
    ), causes


def test_recorded_output_evidence_does_not_depend_on_pipe_chunking() -> None:
    """The same bytes must produce the same size and digest, however split."""

    import hashlib

    from traceh.promotion.verification import _StreamCapture

    def capture(chunks):
        item = _StreamCapture(5)
        for chunk in chunks:
            item.update(chunk)
        return item.size, item.digest, item.exceeded

    whole = capture([b"abcdef"])
    split = capture([b"abc", b"def"])
    byte_at_a_time = capture([bytes([value]) for value in b"abcdef"])
    assert whole == split == byte_at_a_time
    # Exactly the first `limit` bytes are evidence - not the crossing chunk.
    assert whole == (5, hashlib.sha256(b"abcde").hexdigest(), True)


def test_recorded_output_never_exceeds_what_an_outcome_may_carry() -> None:
    """At the largest legal bound, overshoot must not become an input error."""

    from traceh.promotion.models import MAX_OUTPUT_BYTES
    from traceh.promotion.verification import _StreamCapture

    item = _StreamCapture(MAX_OUTPUT_BYTES)
    item.update(b"x" * 65536)
    assert item.size <= MAX_OUTPUT_BYTES
    assert item.exceeded is False


async def test_the_largest_legal_output_bound_still_reports_output_exceeded() -> None:
    """A breach at the maximum bound returns evidence, not a leaked error."""

    import sys
    import tempfile

    from traceh.api.promotion import (
        VerificationPlan,
        VerifierCommand,
        VerifierEnvironmentPolicy,
    )
    from traceh.promotion import HostVerificationRunner
    from traceh.promotion.models import MAX_OUTPUT_BYTES
    from traceh.promotion.verification import _StreamCapture

    # Driving 64 MiB through a real pipe would make this a throughput test, so
    # the boundary itself is exercised directly and the runner is exercised at a
    # bound it can reach quickly.
    at_maximum = _StreamCapture(MAX_OUTPUT_BYTES)
    at_maximum.update(b"y" * 65536)
    assert at_maximum.size == 65536 <= MAX_OUTPUT_BYTES

    plan = VerificationPlan(
        plan_id="bounded-verifier",
        plan_version=1,
        commands=(
            VerifierCommand(
                command_id="floods-stdout",
                argv=(
                    sys.executable,
                    "-c",
                    "import sys\n"
                    "sys.stdout.buffer.write(b'z' * 1048576)\n"
                    "sys.stdout.buffer.flush()\n",
                ),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="bounded-env",
            passthrough=(
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "TMPDIR",
            ),
            overrides=(),
        ),
        max_output_bytes=4096,
        protocol_version=1,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        evidence = await HostVerificationRunner().run(plan, cwd=Path(workdir))
    outcome = evidence.results[0]
    assert outcome.status == "output-exceeded"
    # Deterministic: exactly the configured bound, never the crossing chunk.
    assert outcome.stdout_bytes == 4096, outcome.stdout_bytes
