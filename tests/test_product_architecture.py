"""v0.7-F boundaries: facts/plans stay pure and F3 executes only through seams.

F1 makes ProductTask a durable fact and F2 turns a confirmed one into an exact
plan. F3 adds an explicit host control plane, while the fact/projector/assembly
modules remain non-executing. v0.8-F0 changes only the generic AgentLoop model
admission/dispatch seam and keeps Product outside the kernel.
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
    ProductTaskService,
)

PACKAGE_ROOT = Path(agent_runtime_module.__file__).parent.parent
PRODUCT_ROOT = Path(product_service_module.__file__).parent
WORKFLOW_ROOT = Path(workflow_service_module.__file__).parent

# WC-2: generic fenced cleanup rejoin, proved by lifecycle cancellation
# and reverse tests; no Product dependency enters Supervisor (ADR-0072).
PROTECTED_SOURCES = {
    "runtime/agent_loop.py": ("455be23bf23a5b4b97aa60eabede18a86d1af473bf41e854b9d038ac071eaaf7"),
    "runtime/agent_runtime.py": (
        "51f582071c6beaada35fefbae645d2b6d120ab8a1235252d9a7be1bef8d43851"
    ),
    "supervision/supervisor.py": (
        "b03317a9dbdcd31612ba60dd6e5a1a98e3415ffa6e1d49649b34a9304f105877"
    ),
    # F5: pre-enable Manifest review uses the same loader and activation path.
    "plugins/manager.py": ("f99dc33b0b8be370642383acb64381a0faf536d425dc1fd7fa41a4f4e8086c05"),
}
"""SHA-256 of each protected file with line endings normalized to LF.

ADR-0070 adds a generic source-bound Step view seam only. Product interprets
collaboration phases; the kernel retains its original execution and resource owners.

ADR-0065 binds the existing completion verifier to a host Sandbox scope and
records its receipt; Runtime assembly injects that same service into ToolRuntime.
AgentLoop gains no container operations, Product state, or new execution loop.

ADR-0062 adds only event-derived repeated-denial signals at the Continuation seam;
AgentLoop records config and forwards evidence, without Product or Tool authority.

ADR-0061 AR-B adds HistorySearchTool beside the existing HistoryDisclosureTool,
borrowing the same Session reader and Context policy. No lifecycle or Product grant changes.

ADR-0057 D adds a frozen summary/input source variant and one ordinary summary Step,
sharing the original model permit, Budget, cancellation and lifecycle. No Product
state or parallel Provider execution path is introduced.

These four own the v0.6 concurrency kernel. The product surface is built
entirely above their public seams. v0.8-F0 changes ``AgentLoop`` only to admit
the exact request before Session CAS dispatch permission, bind the resulting
capability back to the host-resolved Provider/Attempt, and converge open
Attempts on generic failure. v0.8-F2 adds same-Step typed retry ownership there
and passes an explicit retry policy through ``AgentRuntime`` composition; no
Product state or dependency enters either file.

v0.9-F4 adds only read/recheck/observation callbacks to AgentLoop and assembles
the qualified Memory reader and optional Git observer in AgentRuntime. Existing
Session, Tool and Lease owners remain; no Product state or new lifecycle enters.

ADR-0053 B adds two default PURE_READ output tools to AgentRuntime, both borrowing
the same SessionService and existing character policy. No AgentLoop, Provider,
Store, Product authority, or lifecycle is added. This is the only pin changed
for retained Tool output; the other three remain fixed.
ADR-0054 B+ adds the third PURE_READ search tool at this same assembly point;
it borrows the same SessionService and source resolver, with no new lifecycle.

ADR-0056 E0 delegates complete request preparation within the existing Lease
to RequestBuilder and assembles an explicit model-bound token estimator. Token
pressure may compact closed older history during the first Step; active Tool
groups, Session dispatch permission and Provider/Budget owners remain unchanged.
No Product dependency, mutable messages or parallel lifecycle is introduced.

v0.9-F3 adds explicit ProjectMemoryConfig, a proposal-only Tool callback and a
host Memory control facade borrowing the existing Lease. No Product import,
authority state machine, Store ownership or lifecycle enters AgentRuntime.

v0.9-F2 passes the exact Skill lease and same-Store read callbacks into Context,
plus a host selection/index facade and an opt-in receipt-only Tool. No Plugin
or SQLite implementation enters AgentLoop; no Product state is introduced.

v0.9-F1 adds SkillPolicy to host assembly and typed Skill registrations to
PluginManager. Catalog/resource receipts follow the existing ActivationSet,
Generation and Lease; no Product dependency or extra lifecycle is introduced.

v0.9-F0-C adds typed host History requests through the same Turn and Session
append owner, plus explicit opt-in registration of a receipt-only PURE_READ
History Tool. No Product knowledge, raw-history writer or new lifecycle enters
either protected module.

v0.9-F0-B adds one read-only Context freeze inside the same Composition Lease,
before its snapshot and outside the same-Step retry loop. AgentRuntime passes
the explicit ContextInputPolicy; Session remains the only append/dispatch
owner and Surface retains no Context. No Product dependency is introduced.

M3 changes both pins again, deliberately. ``AgentLoop`` gains one call to the
Session compaction owner before a Turn opens - the only point where a single
Turn owner exists, no Turn is open and a replacement still precedes every
request this Turn will freeze - plus the non-fatal handling that keeps a failed
compaction from refusing the user's Turn. ``AgentRuntime`` gains the explicit
`CompactionPolicy`, the injected summarizer and the guard that the loop and the
facade share exactly one `CompactionService`. No Product state, model call or
provider dependency enters either file.
Changing any pin is a real architectural decision: update it in the same
commit and say why, rather than deleting the guard.
"""


def _sources(root: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(
        (source, source.read_text(encoding="utf-8")) for source in sorted(root.glob("*.py"))
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
            name.startswith("traceh.product") for name in _imports(Path(module.__file__))
        ), module.__name__
    for source, _ in _sources(WORKFLOW_ROOT):
        assert not any(name.startswith("traceh.product") for name in _imports(source)), source.name


def test_only_cli_and_declared_optimization_owners_depend_on_evaluation() -> None:
    """UE/AO control callers share evaluation; production domain owners do not.

    The three AO modules consume existing contracts/Runner/comparison rather than
    defining a second product surface. Runtime/Product/Workflow retain their own
    success and execution contracts and cannot depend on this outer control layer.
    """

    package = PACKAGE_ROOT
    evaluation_root = package / "evaluation"
    optimization_imports = {
        "optimization_contract.py": {
            "traceh.evaluation.contracts",
            "traceh.evaluation.inputs",
            "traceh.evaluation.variants",
        },
        "optimization.py": {
            "traceh.evaluation.comparison",
            "traceh.evaluation.contracts",
            "traceh.evaluation.inputs",
            "traceh.evaluation.plan",
            "traceh.evaluation.variant_execution",
            "traceh.evaluation.variants",
        },
        "strategy.py": {
            "traceh.evaluation.inputs",
            "traceh.evaluation.model_evidence",
            "traceh.evaluation.model_review",
            "traceh.evaluation.model_service",
            "traceh.evaluation.variant_execution",
            "traceh.evaluation.variants",
        },
        "background_experiment.py": {
            "traceh.evaluation.model_evidence",
            "traceh.evaluation.variants",
        },
    }
    for source in sorted(package.rglob("*.py")):
        if evaluation_root in source.parents:
            continue
        imported = _imports(source)
        referenced = {name for name in imported if name.startswith("traceh.evaluation")}
        if source == package / "cli" / "main.py":
            assert referenced == {
                "traceh.evaluation.errors",
                "traceh.evaluation.runner",
                "traceh.evaluation.plan",
                "traceh.evaluation.review",
                "traceh.evaluation.comparison",
            }, referenced
            continue
        if source.parent == package / "evolution" and source.name in optimization_imports:
            assert referenced == optimization_imports[source.name], source.name
            continue
        if source == package / "chat" / "background.py":
            # AO-3's declared host assembly/parser. Runtime/Product/Workflow
            # still cannot import Evaluation or the optimization scheduler.
            assert referenced == {
                "traceh.evaluation.inputs",
                "traceh.evaluation.model_service",
                "traceh.evaluation.plan",
                "traceh.evaluation.runner",
                "traceh.evaluation.variants",
            }
            continue
        if source == package / "tui" / "optimization_plan.py":
            assert referenced == {
                "traceh.evaluation.evaluators.episode_manifest",
                "traceh.evaluation.manifest",
                "traceh.evaluation.evaluators.product_manifest",
                "traceh.evaluation.plan",
            }
            continue
        assert not referenced, str(source.relative_to(package))
    # And the benchmark never reaches into a private name of the domain it drives.
    for source, text in _sources(evaluation_root):
        assert "._tasks" not in text, source.name
        assert "._control" not in text, source.name
        assert "._execution" not in text, source.name


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
            name.startswith("traceh.workflow.") and name not in PURE_PEER_SYMBOLS
            for name in imported
        ), source.name
        assert not any(
            name.startswith("traceh.promotion.") and name not in PURE_PEER_SYMBOLS
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


def test_the_m2_evidence_tool_holds_only_the_pure_shared_reader() -> None:
    """Detailed memory is a read capability, never a Product control handle."""

    from traceh.api.tools import EffectKind
    from traceh.product.chat import ReadProductTaskEvidenceTool

    assert ReadProductTaskEvidenceTool.__slots__ == ("_memory",)
    assert ReadProductTaskEvidenceTool.effect_kind is EffectKind.PURE_READ
    for filename in ("activity.py", "memory.py"):
        path = PRODUCT_ROOT / filename
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        assert "traceh.product.control" not in imports
        tree = ast.parse(source)
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_store"
            for node in ast.walk(tree)
        )
        for forbidden in (
            "ProductTaskControlPlane",
            "PatchPromotionService",
            "WorkflowService",
        ):
            assert forbidden not in source


def test_the_assembly_service_plans_and_stops() -> None:
    """It produces a receipt. Starting, verifying and promoting are elsewhere."""

    public = {name for name in vars(ProductAssemblyService) if not name.startswith("_")}
    assert public == {"tasks", "preflight", "assemble"}
    for verb in ("start", "run", "execute", "resume", "approve", "promote"):
        assert not any(verb in name for name in public), verb
    assert list(inspect_module.signature(ProductAssemblyService.__init__).parameters) == [
        "self",
        "tasks",
        "registry",
        "sources",
        "targets",
    ]


def test_no_topology_can_arrive_from_configuration() -> None:
    """A Profile chooses who. It has no field with which to choose the graph."""

    for value in (
        product_api.ProductTaskProfile,
        product_api.ProductRoleProfile,
        product_api.ProductTaskProposal,
    ):
        names = {item.name for item in dataclasses.fields(value)}
        for forbidden in ("node", "edge", "graph", "dag", "fan_out", "agents", "count"):
            assert not any(forbidden in name for name in names), (value, forbidden)


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
    # ``session:`` is evidence; ``project-inherit:`` is an opaque operation ID,
    # not a stream. The F3 bridge delegates binding to its existing domain owner.
    assert prefixes <= {"product-task:", "session:", "product:", "project-inherit:"}
    bridge = ast.parse((PRODUCT_ROOT / "project_scope.py").read_text(encoding="utf-8"))
    calls = {ast.unparse(node.func) for node in ast.walk(bridge) if isinstance(node, ast.Call)}
    assert "self.scope.bind_session" in calls
    assert "self.store.append" not in calls
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

    public = {name for name in vars(ProductTaskService) if not name.startswith("_")}
    assert public == {
        "store",
        "load",
        "view",
        "open_task",
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
