"""v0.7-E: the five node kinds against the real public services."""

from __future__ import annotations

import ast
from pathlib import Path

from workflow_fixtures import RecordingResolver, build_assembly, definition

from traceh.api.workflow import (
    AgentTaskNode,
    ApprovalNode,
    JoinNode,
    MapNode,
    NodeStatus,
    VerificationNode,
    WorkflowStatus,
)
from traceh.promotion import expected_approval_digest
from traceh.workflow import workflow_stream_id


async def test_a_single_agent_task_runs_once_and_records_its_identity(
    tmp_path: Path,
) -> None:
    assembly = build_assembly(tmp_path)
    node = AgentTaskNode("build", (), "coder-spec", "coder-message", False)
    run = await assembly.workflow.start("run-1", definition(node))

    assert run.status is WorkflowStatus.COMPLETED
    outcome = run.outcome("build")
    assert outcome is not None
    assert outcome.status is NodeStatus.COMPLETED
    assert outcome.agent_id is not None and outcome.message_id is not None

    # Re-entry must be a fresh read, not a second Agent or a second message.
    before = len(await assembly.store.read(workflow_stream_id("run-1")))
    calls = len(assembly.resolver.message_calls)
    again = await assembly.workflow.resume("run-1", definition(node))
    assert again.status is WorkflowStatus.COMPLETED
    assert len(await assembly.store.read(workflow_stream_id("run-1"))) == before
    assert len(assembly.resolver.message_calls) == calls
    await assembly.aclose()


async def test_agent_identity_is_derived_from_the_run_and_node(
    tmp_path: Path,
) -> None:
    assembly = build_assembly(tmp_path)
    node = AgentTaskNode("build", (), "coder-spec", "coder-message", False)
    first = await assembly.workflow.start("run-1", definition(node))
    second = await assembly.workflow.start("run-2", definition(node))

    one = first.outcome("build")
    two = second.outcome("build")
    assert one is not None and two is not None
    # Same definition and node, different run: different Agent and message.
    assert one.agent_id != two.agent_id
    assert one.message_id != two.message_id
    await assembly.aclose()


async def test_map_records_its_expansion_before_any_child_starts(
    tmp_path: Path,
) -> None:
    resolver = RecordingResolver()
    resolver.keys["fan"] = ("beta", "alpha")
    assembly = build_assembly(tmp_path, resolver=resolver)
    node = MapNode("fan", (), "fan", "coder-spec", "coder-message", 8, False)
    run = await assembly.workflow.start("run-1", definition(node))

    assert run.status is WorkflowStatus.COMPLETED
    parent = run.outcome("fan")
    assert parent is not None
    # Keys are canonicalised, so identity does not depend on host ordering.
    assert parent.map_keys == ("alpha", "beta")

    events = await assembly.store.read(workflow_stream_id("run-1"))
    types = [event.type for event in events]
    expansion = types.index("workflow/map-expanded")
    children = [
        index
        for index, event in enumerate(events)
        if event.type == "workflow/node-started"
        and event.data["map_key"] is not None
    ]
    assert children, types
    assert expansion < min(children), types
    assert len(children) == 2
    await assembly.aclose()


async def test_a_join_waits_for_every_map_child(tmp_path: Path) -> None:
    resolver = RecordingResolver()
    resolver.keys["fan"] = ("alpha", "beta")
    assembly = build_assembly(tmp_path, resolver=resolver)
    nodes = definition(
        MapNode("fan", (), "fan", "coder-spec", "coder-message", 8, False),
        JoinNode("gather", ("fan",)),
    )
    run = await assembly.workflow.start("run-1", nodes)
    assert run.status is WorkflowStatus.COMPLETED

    events = await assembly.store.read(workflow_stream_id("run-1"))
    order = [
        (event.type, event.data.get("node_id"), event.data.get("map_key"))
        for event in events
    ]
    join_start = next(
        index
        for index, item in enumerate(order)
        if item[0] == "workflow/node-started" and item[1] == "gather"
    )
    child_terminals = [
        index
        for index, item in enumerate(order)
        if item[0] == "workflow/node-completed" and item[2] is not None
    ]
    assert len(child_terminals) == 2
    assert max(child_terminals) < join_start, order
    await assembly.aclose()


async def test_verification_binds_the_exact_artifact_and_target(
    tmp_path: Path,
) -> None:
    assembly = build_assembly(tmp_path)
    nodes = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True),
        VerificationNode("review", ("build",), "build", "main-target"),
    )
    run = await assembly.workflow.start("run-1", nodes)

    build = run.outcome("build")
    review = run.outcome("review")
    assert build is not None and build.artifact_id is not None
    assert review is not None and review.status is NodeStatus.COMPLETED
    assert review.artifact_id == build.artifact_id

    ledger = await assembly.promotion.ledger()
    report = ledger.review(review.review_id)
    assert report is not None
    assert report.artifact_id == build.artifact_id
    assert report.target_id == "main-target"
    assert report.passed is True
    await assembly.aclose()


async def test_the_workflow_stops_at_the_approval_barrier(tmp_path: Path) -> None:
    assembly = build_assembly(tmp_path)
    nodes = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True),
        VerificationNode("review", ("build",), "build", "main-target"),
        ApprovalNode("sign", ("review",), "review"),
        AgentTaskNode("after", ("sign",), "coder-spec", "coder-message", False),
    )
    run = await assembly.workflow.start("run-1", nodes)

    assert run.status is WorkflowStatus.AWAITING_APPROVAL
    assert run.awaiting_approval == ("sign",)
    # Nothing after the barrier may have started.
    assert run.outcome("after") is None
    assert run.outcome("sign").status is NodeStatus.RUNNING

    # Re-entering while a human has not decided changes nothing.
    again = await assembly.workflow.resume("run-1", nodes)
    assert again.status is WorkflowStatus.AWAITING_APPROVAL
    assert again.outcome("after") is None
    await assembly.aclose()


async def test_an_approved_review_lets_the_workflow_continue(tmp_path: Path) -> None:
    assembly = build_assembly(tmp_path)
    nodes = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True),
        VerificationNode("review", ("build",), "build", "main-target"),
        ApprovalNode("sign", ("review",), "review"),
    )
    run = await assembly.workflow.start("run-1", nodes)
    assert run.status is WorkflowStatus.AWAITING_APPROVAL

    ledger = await assembly.promotion.ledger()
    report = ledger.review(run.outcome("review").review_id)
    await assembly.promotion.approve(
        review_id=report.review_id,
        approval_digest=expected_approval_digest(report),
        approver_id="release-manager",
        operation_id="approve-1",
    )

    resumed = await assembly.workflow.resume("run-1", nodes)
    assert resumed.status is WorkflowStatus.COMPLETED
    signed = resumed.outcome("sign")
    assert signed.status is NodeStatus.COMPLETED
    assert signed.approval_digest == expected_approval_digest(report)
    await assembly.aclose()


def test_the_workflow_never_approves_or_promotes_on_its_own() -> None:
    """No path through the Workflow may record an approval or move a ref."""

    import traceh.workflow.execution as execution_module
    import traceh.workflow.service as service_module

    for module in (execution_module, service_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "approve" not in calls, module.__name__
        assert "promote" not in calls, module.__name__
        assert "compare_and_swap" not in calls, module.__name__
