"""D2 dependency, authority and future-stage boundary guards."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import get_type_hints

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
        "traceh.promotion.models": {
            "freeze_verification_plan",
            "verifier_definition_digest",
        },
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
    "evaluators/product_manifest.py": {
        "traceh.promotion.models": {"verifier_definition_digest"},
    },
    "evaluators/product_metrics.py": {
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
        if package / "evaluation" in source.parents:
            relative = source.relative_to(package / "evaluation").as_posix()
            assert imports == EVALUATION_PROMOTION_IMPORTS.get(relative, {}), (
                relative,
                imports,
            )
            continue
        if source == package / "tui" / "config_forms.py":
            assert imports == {"traceh.promotion.models": {"PROMOTION_PROTOCOL_VERSION"}}
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



# The old native subprocess/scratch tests moved to the real shared-owner checks
# in test_sandbox_promotion.py and test_sandbox_docker.py. Their private
# _StreamCapture/_execute/shutil seams no longer exist in the fixed verifier.
def test_fixed_verifier_has_no_native_process_or_cleanup_implementation():
    import traceh.promotion.verification as verification

    imported = _imports(verification)
    assert "traceh.sandbox.service" in imported
    assert imported.isdisjoint({"subprocess", "asyncio.subprocess", "shutil", "tempfile"})
