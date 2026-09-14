"""v0.7-F0: the frozen ProductTask contract, and the boundaries it must not cross.

F0 ships a contract, not a product. These tests therefore prove two different
kinds of thing: that the contract says exactly what it claims to say, and that
nothing which belongs to a later stage arrived early.

Where a rule is structural these tests assert the structure. Where a rule is
contract text a later stage must honour, they assert the *shape that makes it
checkable* and say so, rather than pretending a type system enforced it.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

import traceh.api.product as product_module
import traceh.plugins.manager as plugin_manager_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workflow.service as workflow_service_module
from traceh.api.budgets import BudgetLimits
from traceh.api.product import (
    PRODUCT_TASK_ABANDONED,
    PRODUCT_TASK_CANCELLED,
    PRODUCT_TASK_COHERENT_WORKFLOW,
    PRODUCT_TASK_COMPLETED,
    PRODUCT_TASK_EVENT_TYPES,
    PRODUCT_TASK_EVENTS,
    PRODUCT_TASK_FAILED,
    PRODUCT_TASK_OPENED,
    PRODUCT_TASK_PROTOCOL_VERSION,
    PRODUCT_TASK_REJECTED,
    PRODUCT_TASK_SCHEMA_VERSION,
    PRODUCT_TASK_STARTED,
    PRODUCT_TASK_STREAM_PREFIX,
    PRODUCT_TASK_TERMINAL_EVENT_TYPES,
    PRODUCT_TASK_TERMINAL_STATUSES,
    PRODUCT_TASK_TRANSITIONS,
    ProductAssemblyReceipt,
    ProductEventContract,
    ProductPreflightBinding,
    ProductRole,
    ProductRoleProfile,
    ProductTaskFacts,
    ProductTaskProfile,
    ProductTaskProposal,
    ProductTaskReader,
    ProductTaskStatus,
    ProductTaskSummary,
    ProductTaskView,
    ProductTaskViewStatus,
    ProposalConfirmation,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
    product_event_contract,
    product_required_values,
    product_started_mode,
    product_started_values,
    product_transition_allowed,
    product_view_status,
    proposal_confirmable,
)
from traceh.api.workflow import WorkflowStatus
from traceh.api.workspaces import WorkspaceAccess

PACKAGE_ROOT = Path(agent_runtime_module.__file__).parent.parent
PRODUCT_API = Path(product_module.__file__)
WORKFLOW_ROOT = Path(workflow_service_module.__file__).parent

# WC-1B source-view seams were reviewed in ADR-0070; WC-1E leaves these files unchanged.
# WC-2: generic fenced cleanup rejoin, proved by lifecycle cancellation
# and reverse tests; no Product dependency enters Supervisor (ADR-0072).
PROTECTED_SOURCES = {
    "runtime/agent_loop.py": "455be23bf23a5b4b97aa60eabede18a86d1af473bf41e854b9d038ac071eaaf7",
    "runtime/agent_runtime.py": "51f582071c6beaada35fefbae645d2b6d120ab8a1235252d9a7be1bef8d43851",
    "supervision/supervisor.py": "b03317a9dbdcd31612ba60dd6e5a1a98e3415ffa6e1d49649b34a9304f105877",
    "plugins/manager.py": "f99dc33b0b8be370642383acb64381a0faf536d425dc1fd7fa41a4f4e8086c05",
}
"""SHA-256 of each protected file with line endings normalized to LF.

ADR-0065 passes the host Sandbox service through Runtime assembly and delegates
completion verification through its owned scope. No Product authority moves into
AgentLoop; pins match test_product_architecture after reviewing those seams.

ADR-0062 adds only event-derived repeated-denial signals at the Continuation seam;
AgentLoop records config and forwards evidence, without Product or Tool authority.

ADR-0057 D extends the existing model Step with frozen summary/input requests,
sharing admission, Budget and cancellation. E0 metering and D do not add Product
state or a parallel model lifecycle. Pins match test_product_architecture.

These four own the v0.6 concurrency kernel. The product surface is built
entirely above their public seams. v0.8-F0 changes ``AgentLoop`` only at its
generic Model admission/Session dispatch-permit, host Provider/Attempt binding,
and failure-convergence seam. v0.8-F2 adds same-Step typed retry ownership there
and passes an explicit retry policy through ``AgentRuntime`` composition; neither
file gains Product state or a Product dependency.

v0.9-F4 adds only read/recheck/observation callbacks to AgentLoop and assembles
the qualified Memory reader and optional Git observer in AgentRuntime. Existing
Session, Tool and Lease owners remain; no Product state or new lifecycle enters.

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

M3 changes both pins again, deliberately. ``AgentLoop`` invokes the Session
compaction owner before a Turn opens and treats compaction failure as non-fatal;
``AgentRuntime`` owns the explicit policy, summarizer and single shared
``CompactionService`` guard. Neither change introduces Product state, a model
call or a Provider dependency into these protected files.
Normalizing line endings keeps the pin identical on every checkout, whatever
``core.autocrlf`` did locally.

Changing one of these files is a real architectural decision. Update the pin in
the same commit as the change and say why, rather than deleting this guard.
"""

# --------------------------------------------------------------- helpers


def _limits(**overrides: int | None) -> BudgetLimits:
    base: dict[str, int | None] = {
        "max_tokens": 60_000,
        "max_steps": 200,
        "max_tool_calls": 400,
        "max_wall_milliseconds": 900_000,
        "max_children": 3,
        "max_depth": 1,
        "max_processes": 3,
    }
    base.update(overrides)
    return BudgetLimits(**base)  # type: ignore[arg-type]


def _role(name: str) -> ProductRoleProfile:
    return ProductRoleProfile(
        preset=f"preset-{name}",
        capability_grants=("read-workspace",),
        max_output_tokens=4_096,
        budget=_limits(max_children=0, max_depth=0),
        max_turn_wall_milliseconds=60_000,
    )


def _profile() -> ProductTaskProfile:
    return ProductTaskProfile(
        profile_version=1,
        default_mode=RequestedTaskMode.MULTI,
        provider_id="registered-provider",
        model_id="registered-model",
        coder=_role("coder"),
        investigator=_role("investigator"),
        patch_author=None,
        retained_tokens=4_000,
        investigator_initial_tokens=4_000,
        task_budget=_limits(),
        source_id="registered-source",
        source_revision="main",
        verification_plan_id="registered-plan",
        promotion_target_id="registered-target",
    )


def _preflight(profile: ProductTaskProfile) -> ProductPreflightBinding:
    return ProductPreflightBinding(
        profile_digest=profile.digest,
        role_assembly_digest="1" * 64,
        repository_fingerprint="b" * 64,
        base_revision="c" * 40,
        verification_plan_digest="d" * 64,
        promotion_target_fingerprint="e" * 64,
        promotion_target_ref="refs/heads/main",
        promotion_expected_revision="f" * 40,
    )


def _receipt(profile: ProductTaskProfile) -> ProductAssemblyReceipt:
    return ProductAssemblyReceipt(
        preflight=_preflight(profile),
        resolved_mode=ResolvedTaskMode.MULTI,
        workflow_definition_hash="a" * 64,
    )


def _proposal(**overrides: object) -> ProductTaskProposal:
    base: dict[str, object] = {
        "proposal_id": "proposal-1",
        "origin_session_id": "session-1",
        "origin_turn_id": "turn-1",
        "origin_message_id": "message-1",
        "proposed_turn_id": "turn-1",
        "requirement_digest": "1" * 64,
        "requested_mode": RequestedTaskMode.MULTI,
        "mode_source": TaskModeSource.PROFILE,
        "preflight": _preflight(_profile()),
    }
    base.update(overrides)
    return ProductTaskProposal(**base)  # type: ignore[arg-type]


def _confirmation(**overrides: object) -> ProposalConfirmation:
    base: dict[str, object] = {
        "proposal_id": "proposal-1",
        "confirming_session_id": "session-1",
        "confirming_turn_id": "turn-2",
        "confirming_message_id": "message-2",
    }
    base.update(overrides)
    return ProposalConfirmation(**base)  # type: ignore[arg-type]


def _summary(**overrides: object) -> ProductTaskSummary:
    base: dict[str, object] = {
        "task_id": "task-1",
        "status": ProductTaskStatus.OPENED,
        "requested_mode": RequestedTaskMode.MULTI,
        "mode_source": TaskModeSource.PROFILE,
        "requirement_digest": "1" * 64,
        "profile_digest": "2" * 64,
        "preflight_digest": "3" * 64,
        "origin_session_id": "session-1",
        "origin_turn_id": "turn-1",
        "origin_message_id": "message-1",
        "confirmation_session_id": "session-1",
        "confirmation_turn_id": "turn-2",
        "confirmation_message_id": "message-2",
        "head_seq": 1,
    }
    base.update(overrides)
    return ProductTaskSummary(**base)  # type: ignore[arg-type]


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


def _string_literals(text: str) -> set[str]:
    """Every string the module actually uses as a value.

    Docstrings and the bare attribute docs this package writes below its
    constants are excluded: they are prose explaining the contract, and prose
    has to be free to name the very things the contract refuses to encode.
    """

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


def test_the_workflow_does_not_learn_about_the_product_surface() -> None:
    """The dependency runs product -> workflow, and only that way."""

    for source, _ in _sources(WORKFLOW_ROOT):
        assert not any(
            name == "traceh.api.product" or name.startswith("traceh.product")
            for name in _imports(source)
        ), source.name
    for module in (
        agent_loop_module,
        agent_runtime_module,
        supervisor_module,
        plugin_manager_module,
        tools_module,
    ):
        assert not any(
            name == "traceh.api.product" or name.startswith("traceh.product")
            for name in _imports(Path(module.__file__))
        ), module.__name__


def test_the_contract_stays_out_of_the_implementation_that_uses_it() -> None:
    """F0 froze a contract; F1 and F2 implement it in a separate domain.

    The guard that mattered at F0 - "no implementation package exists yet" - was
    overtaken by F1, and its "no router or registry yet" successor by F2. What
    must remain true is the boundary all three were protecting: the contract
    lives in ``traceh.api.product`` and stays free of I/O and state, while
    everything that reads a stream, resolves a Profile or parses a router answer
    lives in ``traceh.product`` and is checked by
    ``tests/test_product_architecture.py``.
    """

    implementation = PACKAGE_ROOT / "product"
    assert implementation.is_dir()
    for name in ("service.py", "registry.py", "assembly.py"):
        assert (implementation / name).exists(), name

    # The contract module still contains no implementation of its own. A
    # Protocol *declaration* of ``load`` is the contract and stays; what must
    # not appear is anything that could touch a stream.
    text = PRODUCT_API.read_text(encoding="utf-8")
    for forbidden in ("EventStore", "PendingEvent", ".append(", ".read("):
        assert forbidden not in text, forbidden

    # F3 lives in the implementation package; it does not move Chat, Workflow
    # or Promotion handles into the dependency-free public DTO module.
    for name in ("chat.py", "control.py", "execution.py", "host.py"):
        assert (implementation / name).exists(), name
    cli_consumers = {
        source.name
        for source in sorted((PACKAGE_ROOT / "cli").glob("*.py"))
        if "traceh.product" in source.read_text(encoding="utf-8")
    }
    # F3's only Product UI remains the existing Chat command: ``main`` assembles
    # the optional host, ``chat`` drives a UI-neutral Turn, and ``product`` is
    # the one Line-terminal adapter. No second command or Product authority is
    # introduced.
    # The existing configuration form validates the same Product host file.
    assert cli_consumers == {"chat.py", "main.py", "product.py", "tui_config.py"}


def test_the_product_api_performs_no_io_and_owns_no_mutable_state() -> None:
    imported = _imports(PRODUCT_API)
    for forbidden in (
        "traceh.session.event_store",
        "traceh.workflow",
        "traceh.promotion",
        "traceh.artifacts",
        "traceh.workspaces",
        "traceh.supervision",
        "traceh.runtime",
        "traceh.cli",
        # A model Tool is a shape, not just a name: not importing the Tool
        # vocabulary is what makes "no product Tool exists" structural.
        "traceh.api.tools",
        "asyncio",
        "os",
        "subprocess",
        "pathlib",
    ):
        assert forbidden not in imported, forbidden
    for name, value in vars(product_module).items():
        if name.startswith("_") or not dataclasses.is_dataclass(value):
            continue
        assert value.__dataclass_params__.frozen, name
        assert getattr(value, "__slots__", None) is not None, name


# --------------------------------------------------- authority boundaries


def test_the_product_api_grants_no_approve_promote_or_tool_capability() -> None:
    """Approval and promotion stay host decisions, and no model may reach them."""

    text = PRODUCT_API.read_text(encoding="utf-8")
    tree = ast.parse(text)
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    for forbidden in ("approve", "promote", "compare_and_swap", "update_ref"):
        assert not any(forbidden in name for name in defined), forbidden

    # The Tool vocabulary is absent by name as well as by import, and the
    # toolset still knows nothing about tasks.
    for forbidden in ("ToolSchema", "EffectKind", "ToolOutput", "PreparedToolCall"):
        assert forbidden not in text, forbidden
    toolset = Path(tools_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("product", "task_id", "propose_task", "start_proposal"):
        assert forbidden not in toolset, forbidden


def test_the_host_rendered_evidence_never_appears_in_a_product_value() -> None:
    """An approval digest, a Patch SHA and a target revision are host surfaces.

    They are what a person checks before authorising a ref move, so they must
    not be reachable through a value the model side of the product API handles.
    """

    fields = {
        field.name
        for value in vars(product_module).values()
        if dataclasses.is_dataclass(value)
        for field in dataclasses.fields(value)
    }
    for forbidden in ("approval_digest", "patch_sha256", "artifact_id"):
        assert forbidden not in fields, forbidden


# ------------------------------------------------------- durable contract


def test_every_product_event_type_is_distinct_and_exactly_shaped() -> None:
    assert len(PRODUCT_TASK_EVENTS) == 8
    assert len(set(PRODUCT_TASK_EVENT_TYPES)) == 8
    for contract in PRODUCT_TASK_EVENTS:
        assert isinstance(contract, ProductEventContract)
        assert contract.event_type.startswith("product/")
        assert contract.schema_version == PRODUCT_TASK_SCHEMA_VERSION
        assert type(contract.keys) is frozenset
        assert {"task_id", "operation_id"} <= contract.keys
    assert product_event_contract(PRODUCT_TASK_OPENED) is not None
    assert product_event_contract("product/task-settled") is None
    assert product_event_contract("workflow/run-started") is None


def test_every_durable_status_is_established_by_exactly_one_event() -> None:
    """The shape contract and the order contract describe the same statuses."""

    by_status = [contract.status for contract in PRODUCT_TASK_EVENTS]
    assert len(set(by_status)) == len(by_status)
    assert set(by_status) == set(ProductTaskStatus)


def test_the_five_ends_are_five_types_not_one_optional_field_blob() -> None:
    """A settled blob would make "completed without a promotion" expressible."""

    terminals = {
        PRODUCT_TASK_COMPLETED,
        PRODUCT_TASK_REJECTED,
        PRODUCT_TASK_CANCELLED,
        PRODUCT_TASK_FAILED,
        PRODUCT_TASK_ABANDONED,
    }
    assert PRODUCT_TASK_TERMINAL_EVENT_TYPES == terminals

    by_type = {
        contract.event_type: contract.keys for contract in PRODUCT_TASK_EVENTS if contract.terminal
    }
    assert by_type[PRODUCT_TASK_COMPLETED] == frozenset({"task_id", "operation_id", "promotion_id"})
    assert by_type[PRODUCT_TASK_REJECTED] == frozenset({"task_id", "operation_id", "review_id"})
    assert by_type[PRODUCT_TASK_FAILED] == frozenset({"task_id", "operation_id", "failure_code"})
    # No terminal may carry another terminal's evidence.
    assert "promotion_id" not in by_type[PRODUCT_TASK_REJECTED]
    assert "review_id" not in by_type[PRODUCT_TASK_COMPLETED]
    assert "failure_code" not in by_type[PRODUCT_TASK_CANCELLED]
    assert "reason_code" not in by_type[PRODUCT_TASK_FAILED]
    # Cancelled and abandoned are different facts about different situations,
    # so neither may be read as the other.
    assert PRODUCT_TASK_CANCELLED != PRODUCT_TASK_ABANDONED


def test_no_product_event_carries_another_domains_state() -> None:
    """The stream keeps references and control decisions, never copies."""

    carried = {key for contract in PRODUCT_TASK_EVENTS for key in contract.keys}
    for forbidden in (
        "final_text",
        "content",
        "patch_sha256",
        "approval_digest",
        "verifier_argv",
        "repository_path",
        "workspace_root",
        "tokens",
        "budget",
        "messages",
        "state",
    ):
        assert forbidden not in carried, forbidden


def test_the_opening_fact_binds_the_protocol_and_the_requirements_origin() -> None:
    opened = product_event_contract(PRODUCT_TASK_OPENED)
    assert opened is not None
    assert "product_protocol_version" in opened.keys
    assert {"origin_session_id", "origin_turn_id", "origin_message_id"} <= opened.keys
    # The requirement itself never enters the stream; only its digest does.
    assert "requirement" not in opened.keys
    assert "requirement_digest" in opened.keys
    assert PRODUCT_TASK_PROTOCOL_VERSION == 6


def test_opening_binds_what_the_person_actually_confirmed() -> None:
    """The screen someone agreed to has to survive into the stream.

    Without this, a Proposal could show one commit, one verification plan and
    one promotion target; the world could move; and ``product/task-started``
    could record a different Assembly Receipt with nothing in the log able to
    contradict it.
    """

    opened = product_event_contract(PRODUCT_TASK_OPENED)
    assert opened is not None
    assert "preflight_digest" in opened.keys
    assert {
        "confirmation_session_id",
        "confirmation_turn_id",
        "confirmation_message_id",
    } <= opened.keys

    # And a started receipt must rest on that exact binding.
    profile = _profile()
    receipt = _receipt(profile)
    assert receipt.binds(receipt.preflight.digest)
    assert not receipt.binds("0" * 64)

    drifted = dataclasses.replace(receipt.preflight, base_revision="9" * 40)
    assert not dataclasses.replace(receipt, preflight=drifted).binds(receipt.preflight.digest)


def test_the_started_fact_binds_the_run_the_definition_and_the_commit() -> None:
    started = product_event_contract(PRODUCT_TASK_STARTED)
    assert started is not None
    assert {
        "workflow_run_id",
        "definition_hash",
        "assembly_digest",
        "source_base_revision",
    } <= started.keys


def test_one_stream_per_task_inside_the_existing_store() -> None:
    assert PRODUCT_TASK_STREAM_PREFIX == "product-task:"
    literals = _string_literals(PRODUCT_API.read_text(encoding="utf-8"))
    streams = {value for value in literals if value.endswith(":")}
    assert streams == {PRODUCT_TASK_STREAM_PREFIX}


# ------------------------------------------------------ status transitions


def test_the_only_first_fact_is_opening_the_task() -> None:
    for status in ProductTaskStatus:
        allowed = product_transition_allowed(None, status, requested_mode=RequestedTaskMode.MULTI)
        assert allowed is (status is ProductTaskStatus.OPENED), status


def test_the_transition_table_cannot_be_rewritten_by_an_importer() -> None:
    """An admission table any caller can edit is not a contract."""

    with pytest.raises(TypeError):
        PRODUCT_TASK_TRANSITIONS[ProductTaskStatus.OPENED] = frozenset()  # type: ignore[index]
    with pytest.raises(AttributeError):
        PRODUCT_TASK_TRANSITIONS.clear()  # type: ignore[attr-defined]
    assert not product_transition_allowed(
        ProductTaskStatus.OPENED,
        ProductTaskStatus.COMPLETED,
        requested_mode=RequestedTaskMode.MULTI,
    )


def test_nothing_may_follow_a_terminal_status() -> None:
    for terminal in PRODUCT_TASK_TERMINAL_STATUSES:
        assert PRODUCT_TASK_TRANSITIONS[terminal] == frozenset()
        for status in ProductTaskStatus:
            for mode in RequestedTaskMode:
                assert not product_transition_allowed(terminal, status, requested_mode=mode), (
                    terminal,
                    status,
                )


def test_no_progress_status_may_repeat() -> None:
    """``routed``, ``started`` and ``awaiting`` happen at most once each."""

    for status in ProductTaskStatus:
        for mode in RequestedTaskMode:
            assert not product_transition_allowed(status, status, requested_mode=mode)


def test_a_review_outcome_requires_having_waited_for_one() -> None:
    """``completed`` and ``rejected`` are reachable only from ``awaiting``."""

    for outcome in (ProductTaskStatus.COMPLETED, ProductTaskStatus.REJECTED):
        assert product_transition_allowed(
            ProductTaskStatus.AWAITING_APPROVAL,
            outcome,
            requested_mode=RequestedTaskMode.MULTI,
        )
        for earlier in (
            ProductTaskStatus.OPENED,
            ProductTaskStatus.STARTED,
        ):
            assert not product_transition_allowed(
                earlier, outcome, requested_mode=RequestedTaskMode.MULTI
            ), (earlier, outcome)

    # And approval can only be awaited once work has actually started.
    assert product_transition_allowed(
        ProductTaskStatus.STARTED,
        ProductTaskStatus.AWAITING_APPROVAL,
        requested_mode=RequestedTaskMode.MULTI,
    )
    for earlier in (ProductTaskStatus.OPENED,):
        assert not product_transition_allowed(
            earlier,
            ProductTaskStatus.AWAITING_APPROVAL,
            requested_mode=RequestedTaskMode.MULTI,
        ), earlier


def test_work_can_stop_at_any_point_before_it_ends() -> None:
    stoppable = (
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    )
    for current in (
        ProductTaskStatus.OPENED,
        ProductTaskStatus.STARTED,
        ProductTaskStatus.AWAITING_APPROVAL,
    ):
        for status in stoppable:
            assert product_transition_allowed(
                current, status, requested_mode=RequestedTaskMode.MULTI
            ), (current, status)


def test_the_sequences_a_shape_only_contract_would_have_accepted() -> None:
    """Concrete counter-examples the transition contract now refuses."""

    mode = RequestedTaskMode.MULTI
    # opened -> completed: an outcome for a review nobody ever waited for.
    assert not product_transition_allowed(
        ProductTaskStatus.OPENED, ProductTaskStatus.COMPLETED, requested_mode=mode
    )
    # awaiting -> started: restarting work that is at the human barrier.
    assert not product_transition_allowed(
        ProductTaskStatus.AWAITING_APPROVAL,
        ProductTaskStatus.STARTED,
        requested_mode=mode,
    )
    # completed -> abandoned: appending after an end.
    assert not product_transition_allowed(
        ProductTaskStatus.COMPLETED, ProductTaskStatus.ABANDONED, requested_mode=mode
    )


# ------------------------------------------------ cross-event value consistency


def test_an_explicit_request_decides_its_own_started_mode() -> None:
    """``single`` cannot start as ``multi``; there is one legal value, derived."""

    for explicit, expected in (
        (RequestedTaskMode.SINGLE, ResolvedTaskMode.SINGLE),
        (RequestedTaskMode.MULTI, ResolvedTaskMode.MULTI),
    ):
        facts = ProductTaskFacts(
            task_id="task-1", requested_mode=explicit, preflight_digest="0" * 64
        )
        assert product_started_mode(facts) is expected
        assert product_required_values(PRODUCT_TASK_STARTED, facts) == {
            "mode": expected.value,
            "workflow_run_id": "task-1",
            "preflight_digest": "0" * 64,
        }


def test_a_rejection_must_name_the_review_that_was_awaited() -> None:
    nothing_awaited = ProductTaskFacts(
        task_id="task-1",
        requested_mode=RequestedTaskMode.MULTI,
        preflight_digest="0" * 64,
    )
    assert product_required_values(PRODUCT_TASK_REJECTED, nothing_awaited) is None

    awaited = dataclasses.replace(nothing_awaited, awaited_review_id="review-9")
    assert product_required_values(PRODUCT_TASK_REJECTED, awaited) == {"review_id": "review-9"}


def test_a_fact_nothing_earlier_decided_carries_no_required_value() -> None:
    facts = ProductTaskFacts(
        task_id="task-1",
        requested_mode=RequestedTaskMode.SINGLE,
        preflight_digest="0" * 64,
    )
    # A promotion id is not knowable from any earlier fact.
    assert product_required_values(PRODUCT_TASK_COMPLETED, facts) == {}
    assert product_required_values(PRODUCT_TASK_OPENED, facts) == {}
    # An unknown type has no contract at all.
    assert product_required_values("product/task-settled", facts) is None


def test_the_established_facts_come_from_the_summary_itself() -> None:
    """One definition of "what is already decided", shared by writer and reader."""

    awaiting = _summary(
        status=ProductTaskStatus.AWAITING_APPROVAL,
        requested_mode=RequestedTaskMode.MULTI,
        resolved_mode=ResolvedTaskMode.MULTI,
        review_id="review-9",
    )
    facts = awaiting.facts()
    assert facts.task_id == awaiting.task_id
    assert facts.requested_mode is RequestedTaskMode.MULTI
    assert facts.resolved_mode is ResolvedTaskMode.MULTI
    assert facts.preflight_digest == awaiting.preflight_digest
    assert facts.awaited_review_id == "review-9"

    # A review id recorded by a *rejection* is not an awaited one.
    settled = _summary(status=ProductTaskStatus.REJECTED, review_id="review-9")
    assert settled.facts().awaited_review_id is None


def test_every_started_value_is_derived_from_one_receipt() -> None:
    """A started payload cannot half-describe one binding and half another.

    Freezing only ``mode`` left ``workflow_run_id``, ``definition_hash``,
    ``assembly_digest`` and ``source_base_revision`` free to name a different
    task, a different definition, another receipt and another commit.
    """

    profile = _profile()
    receipt = _receipt(profile)
    values = product_started_values(task_id="task-1", receipt=receipt)

    assert values == {
        "mode": receipt.resolved_mode.value,
        "workflow_run_id": "task-1",
        "definition_hash": receipt.workflow_definition_hash,
        "assembly_digest": receipt.digest,
        "preflight_digest": receipt.preflight.digest,
        "source_base_revision": receipt.preflight.base_revision,
    }

    # Everything the started fact carries beyond its own write identity.
    started = product_event_contract(PRODUCT_TASK_STARTED)
    assert started is not None
    assert set(values) == started.keys - {"task_id", "operation_id"}

    # A different receipt cannot produce the same payload.
    other = dataclasses.replace(receipt, workflow_definition_hash="9" * 64)
    assert product_started_values(task_id="task-1", receipt=other) != values
    assert product_started_values(task_id="task-2", receipt=receipt) != values


def test_a_replaying_reader_is_told_exactly_what_it_can_check() -> None:
    """Some started relations need the Receipt; the contract must not pretend otherwise.

    A projector holding only events has an opaque ``assembly_digest`` and cannot
    rebuild a Receipt from it. What it *can* do is compare the mode, the run id
    and the preflight digest - which is why the started fact repeats the last of
    those.
    """

    facts = ProductTaskFacts(
        task_id="task-1",
        requested_mode=RequestedTaskMode.MULTI,
        preflight_digest="3" * 64,
    )
    replayable = product_required_values(PRODUCT_TASK_STARTED, facts)
    assert replayable is not None
    assert set(replayable) == {"mode", "workflow_run_id", "preflight_digest"}

    started = product_event_contract(PRODUCT_TASK_STARTED)
    assert started is not None
    needs_receipt = started.keys - set(replayable) - {"task_id", "operation_id"}
    assert needs_receipt == {
        "definition_hash",
        "assembly_digest",
        "source_base_revision",
    }

    # The two derivations agree wherever they overlap.
    profile = _profile()
    receipt = _receipt(profile)
    aligned = ProductTaskFacts(
        task_id="task-1",
        requested_mode=RequestedTaskMode.MULTI,
        preflight_digest=receipt.preflight.digest,
    )
    complete = product_started_values(task_id="task-1", receipt=receipt)
    shared = product_required_values(PRODUCT_TASK_STARTED, aligned)
    assert shared is not None
    for key, value in shared.items():
        assert complete[key] == value, key


def test_the_started_fact_repeats_the_confirmed_preflight_digest() -> None:
    """Both facts carry it, so a reader with no Receipt can still compare them."""

    opened = product_event_contract(PRODUCT_TASK_OPENED)
    started = product_event_contract(PRODUCT_TASK_STARTED)
    assert opened is not None and started is not None
    assert "preflight_digest" in opened.keys
    assert "preflight_digest" in started.keys


# ------------------------------------------------------ derived vs durable


def test_interrupted_is_derived_and_can_never_be_written_down() -> None:
    durable = {status.value for status in ProductTaskStatus}
    view = {status.value for status in ProductTaskViewStatus}
    assert "interrupted" not in durable
    assert view - durable == {"interrupted", "resumable", "unreconciled"}
    assert durable <= view

    # It is not a terminal either: an interrupted task is still un-finished.
    assert "interrupted" not in {status.value for status in PRODUCT_TASK_TERMINAL_STATUSES}
    for contract in PRODUCT_TASK_EVENTS:
        assert "interrupt" not in contract.event_type


def _coherent(status: ProductTaskStatus) -> WorkflowStatus | None:
    return next(iter(PRODUCT_TASK_COHERENT_WORKFLOW.get(status, frozenset({None}))))


def test_a_settled_task_owns_its_own_conclusion() -> None:
    """Nothing else is consulted once a task has durably ended."""

    for status in PRODUCT_TASK_TERMINAL_STATUSES:
        summary = _summary(status=status)
        for workflow in (None, *WorkflowStatus):
            for owned in (True, False):
                assert product_view_status(
                    summary, workflow_status=workflow, owned_by_this_host=owned
                ) is ProductTaskViewStatus(status.value), (status, workflow, owned)


def test_a_live_owner_reports_where_the_task_durably_is() -> None:
    for status in ProductTaskStatus:
        if status in PRODUCT_TASK_TERMINAL_STATUSES:
            continue
        summary = _summary(status=status)
        assert product_view_status(
            summary,
            workflow_status=_coherent(status),
            owned_by_this_host=True,
        ) is ProductTaskViewStatus(status.value), status


def test_the_workflow_state_actually_changes_the_derived_answer() -> None:
    """One unowned STARTED task must not answer the same to every Workflow state.

    An earlier version ignored ``workflow_status`` entirely, so a clean Approval
    barrier, a finished run and a genuinely broken mid-node interruption all
    collapsed into ``interrupted`` - three situations calling for resume,
    reconcile and inspect.
    """

    started = _summary(status=ProductTaskStatus.STARTED)
    answers = {
        workflow: product_view_status(started, workflow_status=workflow, owned_by_this_host=False)
        for workflow in (None, *WorkflowStatus)
    }
    assert answers[WorkflowStatus.RUNNING] is ProductTaskViewStatus.INTERRUPTED
    for moved_on in (
        WorkflowStatus.AWAITING_APPROVAL,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        None,
    ):
        assert answers[moved_on] is ProductTaskViewStatus.UNRECONCILED, moved_on
    assert len(set(answers.values())) > 1


def test_a_clean_approval_barrier_is_resumable_not_interrupted() -> None:
    """The one interrupted state Stage E can continue must be distinguishable."""

    awaiting = _summary(status=ProductTaskStatus.AWAITING_APPROVAL)
    assert (
        product_view_status(
            awaiting,
            workflow_status=WorkflowStatus.AWAITING_APPROVAL,
            owned_by_this_host=False,
        )
        is ProductTaskViewStatus.RESUMABLE
    )

    # A Workflow that already ran past the barrier is a reconciliation, not a resume.
    assert (
        product_view_status(
            awaiting,
            workflow_status=WorkflowStatus.COMPLETED,
            owned_by_this_host=False,
        )
        is ProductTaskViewStatus.UNRECONCILED
    )


def test_a_lagging_product_stream_is_unreconciled_whoever_owns_it() -> None:
    """A live host can find its own stream behind after a failed append."""

    started = _summary(status=ProductTaskStatus.STARTED)
    for owned in (True, False):
        assert (
            product_view_status(
                started,
                workflow_status=WorkflowStatus.AWAITING_APPROVAL,
                owned_by_this_host=owned,
            )
            is ProductTaskViewStatus.UNRECONCILED
        ), owned


def test_a_run_that_exists_too_early_is_also_unreconciled() -> None:
    for early in (ProductTaskStatus.OPENED,):
        assert (
            product_view_status(
                _summary(status=early),
                workflow_status=WorkflowStatus.RUNNING,
                owned_by_this_host=True,
            )
            is ProductTaskViewStatus.UNRECONCILED
        ), early


def test_abandoning_is_legitimate_only_where_the_view_says_interrupted() -> None:
    """The evidence rule, stated as the enumeration it actually is."""

    interrupted = [
        (status, workflow, owned)
        for status in ProductTaskStatus
        for workflow in (None, *WorkflowStatus)
        for owned in (True, False)
        if product_view_status(
            _summary(status=status), workflow_status=workflow, owned_by_this_host=owned
        )
        is ProductTaskViewStatus.INTERRUPTED
    ]
    assert interrupted
    for status, _, owned in interrupted:
        assert status not in PRODUCT_TASK_TERMINAL_STATUSES
        assert owned is False


def test_the_coherence_table_is_frozen_and_covers_every_live_status() -> None:
    live = set(ProductTaskStatus) - PRODUCT_TASK_TERMINAL_STATUSES
    assert set(PRODUCT_TASK_COHERENT_WORKFLOW) == live
    with pytest.raises(TypeError):
        PRODUCT_TASK_COHERENT_WORKFLOW[ProductTaskStatus.OPENED] = frozenset()  # type: ignore[index]


def test_the_derived_view_is_a_different_type_from_the_durable_summary() -> None:
    summary = _summary()
    view = ProductTaskView(summary=summary, workflow_status=None, owned_by_this_host=False)
    assert view.status is ProductTaskViewStatus.INTERRUPTED
    # The view forwards all three reads, not two.
    assert (
        ProductTaskView(
            summary=_summary(status=ProductTaskStatus.AWAITING_APPROVAL),
            workflow_status=WorkflowStatus.AWAITING_APPROVAL,
            owned_by_this_host=False,
        ).status
        is ProductTaskViewStatus.RESUMABLE
    )
    assert view.summary is summary
    assert not hasattr(summary, "owned_by_this_host")
    assert get_type_hints(ProductTaskSummary)["status"] is ProductTaskStatus


def test_the_view_status_cannot_be_supplied_alongside_the_summary() -> None:
    """A suppliable derived status is a second, contradicting copy of the fact.

    As a field it accepted ``summary.status == opened`` beside
    ``view.status == completed``, which is exactly the disagreement this type
    exists to rule out.
    """

    names = {field.name for field in dataclasses.fields(ProductTaskView)}
    assert "status" not in names
    assert isinstance(ProductTaskView.status, property)

    with pytest.raises(TypeError):
        ProductTaskView(  # type: ignore[call-arg]
            summary=_summary(),
            workflow_status=None,
            owned_by_this_host=True,
            status=ProductTaskViewStatus.COMPLETED,
        )

    # And with a coherent Workflow it always agrees with its own summary.
    for status in ProductTaskStatus:
        owned = ProductTaskView(
            summary=_summary(status=status),
            workflow_status=_coherent(status),
            owned_by_this_host=True,
        )
        assert owned.status.value == status.value, status


def test_a_settled_task_is_decided_by_its_durable_status_alone() -> None:
    assert not _summary(status=ProductTaskStatus.AWAITING_APPROVAL).settled
    assert _summary(status=ProductTaskStatus.ABANDONED).settled
    assert _summary(status=ProductTaskStatus.COMPLETED).settled


def test_the_workflow_run_id_is_the_task_id() -> None:
    """One identity, so no mapping exists that could point somewhere else."""

    summary = _summary(task_id="task-87af2c")
    assert summary.workflow_run_id == "task-87af2c"
    assert "workflow_run_id" not in {field.name for field in dataclasses.fields(ProductTaskSummary)}


def test_an_unopened_task_reads_as_nothing_rather_than_as_an_invented_summary() -> None:
    """There is no empty summary, so a reader must not be asked to build one.

    Every required field of a summary is established by ``product/task-opened``.
    A reader that returned a summary for a task with no stream would have to
    invent a status, a mode, two digests and three origin identities that no
    event ever recorded - durable-looking facts with nothing behind them.
    """

    hints = get_type_hints(ProductTaskReader.load)
    assert hints["return"] == (ProductTaskSummary | None)
    assert inspect.iscoroutinefunction(ProductTaskReader.load)

    required = [
        field.name
        for field in dataclasses.fields(ProductTaskSummary)
        if field.default is dataclasses.MISSING
    ]
    assert "status" in required and "requirement_digest" in required
    with pytest.raises(TypeError):
        ProductTaskSummary(task_id="task-1", head_seq=0)  # type: ignore[call-arg]


# --------------------------------------------------------------- profile


def test_the_slot_decides_the_role_and_the_profile_cannot_argue() -> None:
    """A reviewer cannot be handed write authority by shaping its profile.

    ``ProductRoleProfile`` used to carry its own ``role``, so a coder-shaped
    profile placed in the reviewer slot reported ``writable`` for the reviewer.
    Two facts about one role could disagree; now there is only one.
    """

    names = {field.name for field in dataclasses.fields(ProductRoleProfile)}
    assert "role" not in names
    assert "access" not in names
    assert "workspace_access" not in names
    assert not hasattr(ProductRoleProfile, "workspace_access")

    profile = _profile()
    for role in ProductRole:
        assert profile.role_profile(role) is getattr(profile, role.value)

    # Whatever a host puts in the reviewer slot, the reviewer is read-only.
    coder_shaped = dataclasses.replace(profile, investigator=profile.coder)
    assert coder_shaped.role_profile(ProductRole.INVESTIGATOR) is profile.coder
    assert ProductRole.INVESTIGATOR.workspace_access is WorkspaceAccess.READ_ONLY


def test_write_authority_follows_the_role_and_has_one_definition() -> None:
    assert ProductRole.CODER.workspace_access is WorkspaceAccess.WRITABLE
    assert ProductRole.INVESTIGATOR.workspace_access is WorkspaceAccess.READ_ONLY
    writable = [role for role in ProductRole if role.workspace_access is WorkspaceAccess.WRITABLE]
    assert writable == [ProductRole.CODER, ProductRole.PATCH_AUTHOR]

    # ``ProductRole`` is the only value in the module that answers this
    # question. A second answer is how the reviewer got write access before.
    answering = [
        value
        for value in vars(product_module).values()
        if isinstance(getattr(value, "workspace_access", None), property)
    ]
    assert answering == [ProductRole]


def test_the_profile_declares_no_graph_structure() -> None:
    """A Profile chooses who each role is, never what the topology looks like."""

    for value in (ProductTaskProfile, ProductRoleProfile):
        names = {field.name for field in dataclasses.fields(value)}
        for forbidden in (
            "nodes",
            "edges",
            "graph",
            "dag",
            "fan_out",
            "max_fan_out",
            "predecessors",
            "topology",
            "steps",
            "retry",
        ):
            assert forbidden not in names, (value.__name__, forbidden)
        hints = get_type_hints(value)
        for hint in hints.values():
            assert "workflow" not in str(hint).lower(), (value.__name__, hint)


def test_every_budget_dimension_must_be_stated_explicitly() -> None:
    """An omitted host decision cannot become a permissive default."""

    dimensions = [field.name for field in dataclasses.fields(BudgetLimits)]
    assert len(dimensions) == 7
    for field in dataclasses.fields(BudgetLimits):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name

    profile = _profile()
    for accounts in (profile.task_budget, profile.investigator.budget, profile.coder.budget):
        assert [field.name for field in dataclasses.fields(accounts)] == dimensions

    for omitted in dimensions:
        kwargs = {name: 1 for name in dimensions if name != omitted}
        with pytest.raises(TypeError):
            BudgetLimits(**kwargs)  # type: ignore[arg-type]

    # ``None`` remains a real host decision: this dimension is not enforced.
    assert _limits(max_processes=None).max_processes is None


def test_the_profile_digest_covers_every_decision_it_holds() -> None:
    profile = _profile()
    assert profile.digest == _profile().digest
    assert len(profile.digest) == 64

    replacements: dict[str, object] = {
        "profile_version": 2,
        "default_mode": RequestedTaskMode.SINGLE,
        "provider_id": "other-provider",
        "model_id": "other-model",
        "coder": dataclasses.replace(profile.coder, budget=_limits(max_tokens=1)),
        "investigator": dataclasses.replace(profile.investigator, max_turn_wall_milliseconds=10),
        "patch_author": profile.coder,
        "retained_tokens": 8000,
        "investigator_initial_tokens": 3000,
        "task_budget": _limits(max_depth=9),
        "source_id": "other-source",
        "source_revision": "release",
        "verification_plan_id": "other-plan",
        "promotion_target_id": "other-target",
    }
    assert set(replacements) == {field.name for field in dataclasses.fields(ProductTaskProfile)}
    for name, value in replacements.items():
        changed = dataclasses.replace(profile, **{name: value})
        assert changed.digest != profile.digest, name


def test_a_budget_dimension_cannot_be_changed_without_changing_the_digest() -> None:
    profile = _profile()
    for field in dataclasses.fields(BudgetLimits):
        limits = dataclasses.replace(profile.task_budget, **{field.name: 12_345})
        changed = dataclasses.replace(profile, task_budget=limits)
        assert changed.digest != profile.digest, field.name


# ------------------------------------------------------ bindings and receipt


def test_a_name_only_digest_cannot_see_a_registry_rebinding() -> None:
    """Why the resolved assembly digests exist at all.

    A host may keep every preset, provider and model name identical while the
    registry resolves them to a different ``AgentSpec``, different grants or a
    different Tool composition. The profile digest cannot notice, and neither
    can the Workflow definition hash - it covers binding *ids*. Only a digest
    over what was actually resolved closes that gap.
    """

    profile = _profile()
    unchanged_names = _preflight(profile)
    rebound_roles = dataclasses.replace(unchanged_names, role_assembly_digest="9" * 64)
    assert rebound_roles.profile_digest == unchanged_names.profile_digest
    assert rebound_roles.digest != unchanged_names.digest


def test_the_binding_holds_only_non_secret_identities_and_exact_revisions() -> None:
    binding = _preflight(_profile())
    hints = get_type_hints(ProductPreflightBinding)
    for name, hint in hints.items():
        assert hint is str, (name, hint)
    names = {field.name for field in dataclasses.fields(ProductPreflightBinding)}
    for forbidden in (
        "repository_path",
        "root",
        "path",
        "api_key",
        "token",
        "base_url",
        "environment",
        "argv",
        "command",
    ):
        assert forbidden not in names, forbidden
    assert binding.base_revision == "c" * 40
    assert binding.promotion_target_ref == "refs/heads/main"
    assert binding.promotion_expected_revision == "f" * 40


def test_a_proposal_does_not_claim_an_executed_workflow() -> None:
    """A confirmed mode is not proof that its Workflow has started."""

    preflight_fields = {field.name for field in dataclasses.fields(ProductPreflightBinding)}
    assert "resolved_mode" not in preflight_fields
    assert "workflow_definition_hash" not in preflight_fields

    receipt_fields = {field.name for field in dataclasses.fields(ProductAssemblyReceipt)}
    assert receipt_fields == {"preflight", "resolved_mode", "workflow_definition_hash"}

    proposal_hints = get_type_hints(ProductTaskProposal)
    assert proposal_hints["preflight"] is ProductPreflightBinding
    assert proposal_hints["requested_mode"] is RequestedTaskMode
    assert "resolved_mode" not in proposal_hints


def test_the_assembly_digest_covers_every_binding_it_records() -> None:
    profile = _profile()
    receipt = _receipt(profile)
    assert receipt.digest == _receipt(profile).digest
    assert len(receipt.digest) == 64

    replacements: dict[str, object] = {
        "preflight": dataclasses.replace(receipt.preflight, base_revision="9" * 40),
        "resolved_mode": ResolvedTaskMode.SINGLE,
        "workflow_definition_hash": "1" * 64,
    }
    assert set(replacements) == {field.name for field in dataclasses.fields(ProductAssemblyReceipt)}
    for name, value in replacements.items():
        changed = dataclasses.replace(receipt, **{name: value})
        assert changed.digest != receipt.digest, name

    # Every preflight field reaches the receipt digest.
    for field in dataclasses.fields(ProductPreflightBinding):
        moved = dataclasses.replace(receipt.preflight, **{field.name: "7" * 64})
        assert dataclasses.replace(receipt, preflight=moved).digest != receipt.digest, field.name


def test_a_profile_change_reaches_the_assembly_digest() -> None:
    """Coverage of provider, presets, grants and budgets is transitive but real."""

    profile = _profile()
    baseline = _receipt(profile)
    for changed_profile in (
        dataclasses.replace(profile, provider_id="other-provider"),
        dataclasses.replace(profile, model_id="other-model"),
        dataclasses.replace(
            profile, coder=dataclasses.replace(profile.coder, preset="other-preset")
        ),
        dataclasses.replace(
            profile,
            investigator=dataclasses.replace(profile.investigator, capability_grants=()),
        ),
        dataclasses.replace(profile, task_budget=_limits(max_tool_calls=1)),
    ):
        assert _receipt(changed_profile).digest != baseline.digest


def test_a_digest_cannot_be_supplied_instead_of_derived() -> None:
    """A stored digest is a second place the same fact can disagree with itself."""

    for value in (
        ProductTaskProfile,
        ProductPreflightBinding,
        ProductAssemblyReceipt,
    ):
        names = {field.name for field in dataclasses.fields(value)}
        assert "digest" not in names, value.__name__
        assert isinstance(value.digest, property), value.__name__


# -------------------------------------------------------------- proposal


def test_a_proposal_is_a_value_no_event_can_carry() -> None:
    """It is a question; the honest record of an unanswered question is none."""

    carried = {key for contract in PRODUCT_TASK_EVENTS for key in contract.keys}
    for forbidden in ("proposal_id", "proposal", "preflight"):
        assert forbidden not in carried, forbidden
    assert not any("proposal" in contract.event_type for contract in PRODUCT_TASK_EVENTS)


def test_confirming_requires_the_exact_proposal() -> None:
    proposal = _proposal()
    assert proposal_confirmable(proposal, _confirmation())
    # A stale confirmation cannot accept a Proposal that has been replaced.
    assert not proposal_confirmable(proposal, _confirmation(proposal_id="proposal-0"))
    # A real requirement message is not also evidence that the offer was accepted.
    assert not proposal_confirmable(proposal, _confirmation(confirming_message_id="message-1"))


def test_confirmation_comparisons_use_plain_values_not_hostile_string_equality() -> None:
    """A caller-controlled ``str`` subclass cannot manufacture human consent."""

    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            del other
            return True

        def __ne__(self, other: object) -> bool:  # noqa: D105
            del other
            return False

        __hash__ = str.__hash__

    proposal = _proposal(origin_session_id="session-alpha")
    forged = _confirmation(
        proposal_id=EqualToEverything("proposal-other"),
        confirming_session_id=EqualToEverything("session-beta"),
    )

    assert not proposal_confirmable(proposal, forged)

    class Disguised(str):
        def __new__(cls, value: str, disguise: str):
            instance = super().__new__(cls, value)
            instance.disguise = disguise
            return instance

        def __str__(self) -> str:  # noqa: D105
            return self.disguise

    stateful = _confirmation(
        proposal_id=Disguised("proposal-other", "proposal-1"),
        confirming_session_id=Disguised("session-beta", "session-alpha"),
    )
    assert not proposal_confirmable(proposal, stateful)


def test_a_confirmation_from_another_conversation_is_not_this_person() -> None:
    proposal = _proposal()
    assert not proposal_confirmable(proposal, _confirmation(confirming_session_id="session-9"))


def test_a_model_cannot_propose_and_confirm_in_one_breath() -> None:
    """The Turn that must differ is the one that made the offer.

    An earlier version compared against ``origin_turn_id`` - where the
    *requirement* was stated. Those are routinely different Turns: a user asks a
    question, gets an answer, then says "alright, do it", and the model proposes
    during that later Turn. Comparing the wrong one let a model propose in
    Turn 2 and confirm in Turn 2.
    """

    proposal = _proposal(origin_turn_id="turn-1", proposed_turn_id="turn-2")
    assert not proposal_confirmable(proposal, _confirmation(confirming_turn_id="turn-2"))
    assert proposal_confirmable(proposal, _confirmation(confirming_turn_id="turn-3"))

    # The requirement Turn is not the barrier, so reusing it is not what matters.
    same_turn = _proposal(origin_turn_id="turn-1", proposed_turn_id="turn-1")
    assert not proposal_confirmable(same_turn, _confirmation(confirming_turn_id="turn-1"))
    assert proposal_confirmable(same_turn, _confirmation(confirming_turn_id="turn-2"))


def test_a_proposal_records_where_the_offer_was_made_separately() -> None:
    names = {field.name for field in dataclasses.fields(ProductTaskProposal)}
    assert "proposed_turn_id" in names
    assert "origin_turn_id" in names


def test_a_confirmation_has_no_field_with_which_to_change_the_offer() -> None:
    """The low-privilege operation cannot become a high-privilege one."""

    names = {field.name for field in dataclasses.fields(ProposalConfirmation)}
    assert names == {
        "proposal_id",
        "confirming_session_id",
        "confirming_turn_id",
        "confirming_message_id",
    }
    for forbidden in (
        "mode",
        "budget",
        "source_id",
        "revision",
        "target_id",
        "verification_plan_id",
        "profile",
        "preflight",
    ):
        assert forbidden not in names, forbidden


def test_a_proposal_binds_the_requirement_and_the_resolved_world() -> None:
    proposal = _proposal()
    names = {field.name for field in dataclasses.fields(ProductTaskProposal)}
    assert {"requirement_digest", "preflight", "requested_mode", "mode_source"} <= names
    assert {"origin_session_id", "origin_turn_id", "origin_message_id"} <= names
    # The requirement text itself is never carried, only its digest.
    assert "requirement" not in names
    assert proposal.preflight.base_revision == "c" * 40


# --------------------------------------------------------- no leaked values


def test_no_example_identity_leaks_into_the_contract() -> None:
    """A demo, a model name or a local path must never become a general value."""

    literals = _string_literals(PRODUCT_API.read_text(encoding="utf-8"))
    for forbidden in (
        "gpt",
        "claude",
        "openai",
        "deepseek",
        "demo",
        "example",
        "sample",
        "pytest",
        "tmp_path",
        "C:\\",
        "/home/",
        "/tmp",
        "localhost",
        "http://",
        "https://",
        "secret",
        "token",
        "password",
        "api_key",
        ".env",
        "main-target",
        "coder-spec",
    ):
        assert not any(forbidden in literal for literal in literals), forbidden

    for value in vars(product_module).values():
        if not dataclasses.is_dataclass(value):
            continue
        for field in dataclasses.fields(value):
            if field.name in ("keys", "status", "schema_version", "event_type"):
                continue
            assert field.default is dataclasses.MISSING or field.default is None, (
                value.__name__,
                field.name,
            )


def test_only_explicit_single_and_multi_modes_are_supported() -> None:
    assert {mode.value for mode in RequestedTaskMode} == {"single", "multi"}
    assert {mode.value for mode in ResolvedTaskMode} == {"single", "multi"}
    for legacy in ("auto", "adaptive"):
        with pytest.raises(ValueError):
            RequestedTaskMode(legacy)
        with pytest.raises(ValueError):
            ResolvedTaskMode(legacy)
    for mode in RequestedTaskMode:
        assert product_transition_allowed(
            ProductTaskStatus.OPENED, ProductTaskStatus.STARTED, requested_mode=mode
        )
    assert product_event_contract("product/task-routed") is None
