"""v0.7-F1 boundaries: what the ProductTask domain may reach, and what it may not.

F1 makes ProductTask a durable fact. It does not make anything happen. These
tests pin that difference, because it is exactly the boundary a later stage will
be tempted to blur.
"""

from __future__ import annotations

import ast
import hashlib
import inspect as inspect_module
from pathlib import Path

import traceh.plugins.manager as plugin_manager_module
import traceh.product.service as product_service_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workflow.service as workflow_service_module
from traceh.product import ProductTaskService
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


def test_the_product_domain_executes_nothing_it_records() -> None:
    """F1 writes facts about work. It does not start, verify or promote any."""

    forbidden = {
        "traceh.workflow",
        "traceh.workflow.service",
        "traceh.promotion",
        "traceh.promotion.service",
        "traceh.artifacts",
        "traceh.workspaces",
        "traceh.supervision",
        "traceh.supervision.supervisor",
        "traceh.runtime",
        "traceh.runtime.agent_loop",
        "traceh.runtime.agent_runtime",
        "traceh.plugins",
        "traceh.plugins.manager",
        "traceh.cli",
        "traceh.evolution",
        "traceh.llm",
    }
    for source, _ in _sources(PRODUCT_ROOT):
        imported = _imports(source)
        assert forbidden.isdisjoint(imported), (source.name, forbidden & imported)


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


def test_no_chat_command_or_router_arrived_early() -> None:
    """F2 owns the Router; the chat surface is later still."""

    for source, text in _sources(PRODUCT_ROOT):
        for forbidden in (
            "TaskRoutingParser",
            "propose_task",
            "start_proposal",
        ):
            assert forbidden not in text, (source.name, forbidden)
        command_literals = {
            "/approve",
            "/start",
            "/cancel",
            "/inspect",
            "/abandon",
        }
        assert command_literals.isdisjoint(_literals(text)), source.name
    package = PACKAGE_ROOT
    for source, text in _sources(package / "cli"):
        assert "traceh.product" not in text, source.name
        assert "ProductTaskService" not in text, source.name


def test_the_domain_grants_no_approve_promote_or_capture_capability() -> None:
    defined: set[str] = set()
    for _source, text in _sources(PRODUCT_ROOT):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
    for forbidden in ("approve", "promote", "compare_and_swap", "capture"):
        assert not any(forbidden in name for name in defined), forbidden
    toolset = Path(tools_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("product", "task_id", "ProductTask"):
        assert forbidden not in toolset, forbidden


def test_the_product_task_stream_is_the_only_new_fact_source() -> None:
    """No status file, database or second store - one prefix, one stream shape."""

    prefixes: set[str] = set()
    for _, text in _sources(PRODUCT_ROOT):
        for value in _literals(text):
            if value.endswith(":"):
                prefixes.add(value)
    # ``session:`` is read-only evidence, never written by this domain.
    assert prefixes <= {"product-task:", "session:"}
    for source, text in _sources(PRODUCT_ROOT):
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
