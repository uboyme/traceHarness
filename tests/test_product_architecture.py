"""v0.7-F boundaries: facts/plans stay pure and F3 executes only through seams.

F1 makes ProductTask a durable fact and F2 turns a confirmed one into an exact
plan. F3 adds an explicit host control plane, while the fact/projector/assembly
modules remain non-executing and the four v0.6 kernel owners remain unchanged.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect as inspect_module
from pathlib import Path

import traceh.api.product as product_api
import traceh.plugins.manager as plugin_manager_module
import traceh.product.service as product_service_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workflow.service as workflow_service_module
from traceh.product import (
    ProductAssemblyService,
    ProductModeRouter,
    ProductTaskService,
    RouterResponder,
    StrictTaskRoutingParser,
)
from traceh.product.router import ROUTER_RESPONSE_KEYS
from traceh.version import __version__

PACKAGE_ROOT = Path(agent_runtime_module.__file__).parent.parent
PRODUCT_ROOT = Path(product_service_module.__file__).parent
WORKFLOW_ROOT = Path(workflow_service_module.__file__).parent

PROTECTED_SOURCES = {
    "runtime/agent_loop.py": (
        "9a00d94ca8d400c55c0746c5f5d67d9b541f9a30f29ae602828d40c8b3f954e0"
    ),
    "runtime/agent_runtime.py": (
        "fc76d8a4eb6f953da4e61eb81140a0748ef1d4fc4e15c0fc876cfcc02e83ae92"
    ),
    "supervision/supervisor.py": (
        "acc23496367dbe2088021f5d61ca619cc03e0ae0da97c271efa547dfbd5009a0"
    ),
    "plugins/manager.py": (
        "b09c23e0a2f9a76cf359187785463fd032d3204f8f5d8b8b6c79e601b5ab6eb9"
    ),
}
"""SHA-256 of each protected file with line endings normalized to LF.

These four own the v0.6 concurrency kernel. The product surface is built
entirely above their public seams, so v0.7-F must not need a byte of them.
Changing one is a real architectural decision: update the pin in the same
commit and say why, rather than deleting the guard.
"""


def _sources(root: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(
        (source, source.read_text(encoding="utf-8"))
        for source in sorted(root.glob("*.py"))
    )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def _literals(text: str) -> set[str]:
    tree = ast.parse(text)
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    }


# ------------------------------------------------------- stage boundaries


def test_the_four_protected_files_are_byte_identical() -> None:
    for relative, expected in PROTECTED_SOURCES.items():
        raw = (PACKAGE_ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == expected, relative


def test_the_package_version_is_unchanged() -> None:
    """F1 ships a fact layer, not a release."""

    assert __version__ == "0.6.0"


def test_no_existing_owner_learns_about_the_product_domain() -> None:
    """The dependency runs product -> everything else, never back."""

    for module in (
        agent_loop_module,
        agent_runtime_module,
        supervisor_module,
        plugin_manager_module,
        tools_module,
    ):
        assert not any(
            name.startswith("traceh.product")
            for name in _imports(Path(module.__file__))
        ), module.__name__
    for source, _ in _sources(WORKFLOW_ROOT):
        assert not any(
            name.startswith("traceh.product") for name in _imports(source)
        ), source.name


EXECUTING_MODULES = {
    "traceh.workflow",
    "traceh.workflow.service",
    "traceh.workflow.execution",
    "traceh.workflow.projection",
    "traceh.promotion",
    "traceh.promotion.service",
    "traceh.promotion.verification",
    "traceh.promotion.local_git",
    "traceh.promotion.projection",
    "traceh.artifacts",
    "traceh.workspaces",
    "traceh.workspaces.local_git",
    "traceh.workspaces.service",
    "traceh.supervision",
    "traceh.supervision.supervisor",
    "traceh.supervision.execution",
    "traceh.runtime",
    "traceh.runtime.agent_loop",
    "traceh.runtime.agent_runtime",
    "traceh.plugins",
    "traceh.plugins.manager",
    "traceh.cli",
    "traceh.evolution",
    "traceh.llm",
}
"""Everything that owns an Agent, a worktree, a Git repository or a run."""

PURE_PEER_SYMBOLS = {
    "traceh.workflow.models": {
        "freeze_workflow_definition",
        "workflow_definition_hash",
    },
    "traceh.promotion.models": {
        "freeze_verification_plan",
        "require_target_ref",
        "verifier_definition_digest",
    },
}
"""The named pure symbols F2 reuses instead of writing a second definition of.

A product receipt records *the* Workflow definition hash and *the* verifier
definition digest. Computing either one here would create a second answer to a
question another domain already owns, and the two would drift the first time
either changed. The target ref likewise uses the Promotion domain's one syntax
rule rather than a weaker Product copy. So exactly these five functions are
allowed - none of them reads
a store, starts a run or touches a repository - and nothing else from those
modules is. This follows the precedent the Workflow domain set for
``durable_log_identity``.
"""

PLANNING_FILES = {
    "assembly.py",
    "errors.py",
    "events.py",
    "evidence.py",
    "projection.py",
    "registry.py",
    "router.py",
    "service.py",
    "topology.py",
}


def test_the_product_domain_executes_nothing_it_records() -> None:
    """The domain writes facts about work and plans it. It runs none of it."""

    for source, _ in _sources(PRODUCT_ROOT):
        if source.name not in PLANNING_FILES:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = _imports(source)
        assert EXECUTING_MODULES.isdisjoint(imported), (
            source.name,
            EXECUTING_MODULES & imported,
        )
        for module, allowed in PURE_PEER_SYMBOLS.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == module:
                    names = {alias.name for alias in node.names}
                    assert names <= allowed, (source.name, module, names)
        assert not any(
            name.startswith("traceh.workflow.")
            and name not in PURE_PEER_SYMBOLS
            for name in imported
        ), source.name
        assert not any(
            name.startswith("traceh.promotion.")
            and name not in PURE_PEER_SYMBOLS
            for name in imported
        ), source.name


def test_the_domain_calls_no_service_that_would_make_something_happen() -> None:
    """Import guards catch a module; this catches the name that would use one."""

    forbidden = (
        "WorkflowService",
        "PatchPromotionService",
        "PatchCaptureService",
        "ProcessAgentSupervisor",
        "AgentSupervisor",
        "AgentRuntime",
        "WorkspaceService",
        "PluginManager",
    )
    for source, text in _sources(PRODUCT_ROOT):
        if source.name not in PLANNING_FILES:
            continue
        for name in forbidden:
            assert name not in text, (source.name, name)


def test_the_product_domain_reuses_the_shared_rules_it_needs() -> None:
    """Reusing them is the point; a private weakened copy would be the failure."""

    reused = set()
    for source, _ in _sources(PRODUCT_ROOT):
        reused |= _imports(source)
    assert "traceh.agents.commit_reconciliation" in reused
    assert "traceh.agents.identity" in reused
    assert "traceh.concurrency" in reused
    assert "traceh.api.product" in reused
    assert "traceh.session.invariants" in reused


def test_the_f3_model_tools_hold_only_ephemeral_turn_actions() -> None:
    """Proposal Tools cannot reach execution, Review, approval or promotion."""

    from traceh.product.chat import ConfirmProductTaskTool, ProposeProductTaskTool

    assert ProposeProductTaskTool.__slots__ == ("_actions",)
    assert ConfirmProductTaskTool.__slots__ == ("_actions",)
    source = (PRODUCT_ROOT / "chat.py").read_text(encoding="utf-8")
    imported = _imports(PRODUCT_ROOT / "chat.py")
    assert "traceh.workflow" not in imported
    assert "traceh.promotion.service" not in imported
    for forbidden in ("PatchPromotionService", "WorkflowService", "AgentSupervisor"):
        assert forbidden not in source


def test_the_router_seam_receives_text_and_returns_a_decision() -> None:
    """No handle reaches the router *through* the seam - it is handed a string."""

    responder = inspect_module.signature(RouterResponder.respond)
    assert list(responder.parameters) == ["self", "summary", "task_id"]
    assert responder.parameters["summary"].annotation == "str"
    parser = inspect_module.signature(StrictTaskRoutingParser.parse)
    assert list(parser.parameters) == ["self", "response"]
    assert parser.parameters["response"].annotation == "str"
    assert list(inspect_module.signature(ProductModeRouter.route).parameters) == [
        "self",
        "summary",
        "task_id",
    ]


def test_the_assembly_service_plans_and_stops() -> None:
    """It produces a receipt. Starting, verifying and promoting are elsewhere."""

    public = {
        name for name in vars(ProductAssemblyService) if not name.startswith("_")
    }
    assert public == {"tasks", "preflight", "assemble"}
    for verb in ("start", "run", "execute", "resume", "approve", "promote"):
        assert not any(verb in name for name in public), verb
    assert list(
        inspect_module.signature(ProductAssemblyService.__init__).parameters
    ) == ["self", "tasks", "registry", "sources", "targets", "router"]


def test_no_topology_can_arrive_from_configuration() -> None:
    """A Profile chooses who. It has no field with which to choose the graph."""

    for value in (
        product_api.ProductTaskProfile,
        product_api.ProductRoleProfile,
        product_api.ProductRouterProfile,
        product_api.TaskRouting,
        product_api.ProductTaskProposal,
    ):
        names = {item.name for item in dataclasses.fields(value)}
        for forbidden in ("node", "edge", "graph", "dag", "fan_out", "agents", "count"):
            assert not any(forbidden in name for name in names), (value, forbidden)
    assert set(ROUTER_RESPONSE_KEYS) == {"mode", "reason"}


def test_generic_agent_tools_gain_no_product_authority() -> None:
    toolset = Path(tools_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("product", "task_id", "ProductTask"):
        assert forbidden not in toolset, forbidden


def test_the_product_task_stream_is_the_only_new_fact_source() -> None:
    """No status file, database or second store - one prefix, one stream shape."""

    prefixes: set[str] = set()
    for _, text in _sources(PRODUCT_ROOT):
        for value in _literals(text):
            if (
                value.endswith(":")
                and value == value.strip()
                and " " not in value
                and value == value.lower()
            ):
                prefixes.add(value)
    # ``session:`` is read-only evidence, never written by this domain.
    assert prefixes <= {"product-task:", "session:", "product:"}
    fact_files = {"events.py", "projection.py", "service.py"}
    for source, text in _sources(PRODUCT_ROOT):
        if source.name not in fact_files:
            continue
        for forbidden in ("sqlite", "json.dump", "open(", "Path(", "shelve"):
            assert forbidden not in text, (source.name, forbidden)


def test_no_test_fixture_identity_leaks_into_production_code() -> None:
    forbidden = (
        "task-1",
        "session-alpha",
        "proposal-1",
        "review-1",
        "registered-",
        "preset-",
        "pytest",
        "tmp_path",
        "example",
    )
    for source, text in _sources(PRODUCT_ROOT):
        literals = _literals(text)
        for value in forbidden:
            assert not any(value in literal for literal in literals), (
                source.name,
                value,
            )


def test_the_service_seams_stay_narrow() -> None:
    """A view must ask for its inputs, not be handed a cached answer."""

    signature = inspect_module.signature(ProductTaskService.__init__)
    assert set(signature.parameters) == {
        "self",
        "store",
        "sessions",
        "workflow",
        "ownership",
    }
    # The view takes nothing but the task: everything else is read fresh.
    assert list(inspect_module.signature(ProductTaskService.view).parameters) == [
        "self",
        "task_id",
    ]


def test_the_service_cannot_continue_anything() -> None:
    """Stage E decides what may be continued; F1 records, it does not resume.

    A substring scan would be the wrong guard - ``resumable`` is a legitimate
    *derived* answer this domain reports. What must not exist is an operation
    that acts on it.
    """

    public = {
        name
        for name in vars(ProductTaskService)
        if not name.startswith("_")
    }
    assert public == {
        "store",
        "load",
        "view",
        "open_task",
        "record_routing",
        "start_task",
        "record_awaiting",
        "complete_task",
        "reject_task",
        "cancel_task",
        "fail_task",
        "abandon_task",
        "aclose",
    }
    for verb in ("resume", "continue", "recover", "takeover", "retry"):
        assert not any(verb in name for name in public), verb
