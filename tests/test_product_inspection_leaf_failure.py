"""Read-only Product inspection of the failed role's Session leaf."""

from __future__ import annotations

from pathlib import Path

import pytest

from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    AGENT_EVENT_SCHEMA_VERSION,
    agent_created_data,
)
from traceh.api.agents import AgentSpec
from traceh.api.events import PendingEvent
from traceh.api.product import (
    ProductRole,
    ProductTaskStatus,
    ProductTaskSummary,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.promotion import VerificationPlan, VerifierEnvironmentPolicy
from traceh.api.workflow import NodeStatus
from traceh.artifacts import LocalArtifactCas
from traceh.artifacts.reader import PatchArtifactReader
from traceh.product.errors import ProductStateError
from traceh.product.inspection import ProductInspectionEvidenceReader
from traceh.product.topology import (
    product_role_node_id,
    product_workflow_definition,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.workflow.events import (
    WORKFLOW_NODE_FAILED,
    WORKFLOW_NODE_STARTED,
    WORKFLOW_RUN_FINISHED,
    WORKFLOW_RUN_STARTED,
    node_failed_data,
    node_started_data,
    run_finished_data,
    run_started_data,
    workflow_stream_id,
)
from traceh.workflow.models import agent_identity, node_kind, workflow_definition_hash

_TASK_ID = "inspection-task"
_TURN_ID = "inspection-turn"
_STEP_ID = "inspection-step"


def _summary() -> ProductTaskSummary:
    return ProductTaskSummary(
        task_id=_TASK_ID,
        status=ProductTaskStatus.FAILED,
        requested_mode=RequestedTaskMode.SINGLE,
        mode_source=TaskModeSource.CONFIRMED_PROPOSAL,
        requirement_digest="1" * 64,
        profile_digest="2" * 64,
        preflight_digest="3" * 64,
        origin_session_id="origin-session",
        origin_turn_id="origin-turn",
        origin_message_id="origin-message",
        confirmation_session_id="origin-session",
        confirmation_turn_id="confirmation-turn",
        confirmation_message_id="confirmation-message",
        head_seq=4,
        resolved_mode=ResolvedTaskMode.SINGLE,
        failure_code="product-task-failed",
    )


async def _reader(
    store: InMemoryEventStore,
    root: Path,
    *,
    node_failure_code: str = "workflow-agent-message-failed",
    request_id_override: str | None = None,
    agent_preset: str = "inspection-role",
) -> tuple[ProductInspectionEvidenceReader, str, str]:
    summary = _summary()
    definition = product_workflow_definition(
        ResolvedTaskMode.SINGLE, promotion_target_id="integration-target"
    )
    coder_id = product_role_node_id(ProductRole.CODER)
    coder = next(node for node in definition.nodes if node.node_id == coder_id)
    await store.append(
        workflow_stream_id(summary.workflow_run_id),
        expected_seq=0,
        events=(
            PendingEvent(
                WORKFLOW_RUN_STARTED,
                run_started_data(
                    run_id=summary.workflow_run_id,
                    definition_id=definition.definition_id,
                    definition_hash=workflow_definition_hash(definition),
                ),
            ),
            PendingEvent(
                WORKFLOW_NODE_STARTED,
                node_started_data(
                    node_id=coder_id, kind=node_kind(coder), map_key=None
                ),
            ),
            PendingEvent(
                WORKFLOW_NODE_FAILED,
                node_failed_data(
                    node_id=coder_id,
                    kind=node_kind(coder),
                    map_key=None,
                    failure_code=node_failure_code,
                ),
            ),
            PendingEvent(
                WORKFLOW_RUN_FINISHED,
                run_finished_data(
                    status=NodeStatus.FAILED.value,
                    failure_code="workflow-node-failed",
                ),
            ),
        ),
    )
    agent_id, session_id, request_id, message_id = agent_identity(
        summary.workflow_run_id, coder_id
    )
    await store.append(
        AGENT_DIRECTORY_STREAM,
        expected_seq=0,
        events=(
            PendingEvent(
                AGENT_CREATED,
                agent_created_data(
                    agent_id=agent_id,
                    session_id=session_id,
                    request_id=request_id_override or request_id,
                    spec=AgentSpec(
                        preset=agent_preset,
                        workspace_id="inspection-workspace",
                    ),
                ),
                schema_version=AGENT_EVENT_SCHEMA_VERSION,
            ),
        ),
    )
    plan = VerificationPlan(
        plan_id="inspection-plan",
        plan_version=1,
        commands=(),
        environment=VerifierEnvironmentPolicy(
            policy_id="inspection-environment", passthrough=(), overrides=()
        ),
        max_output_bytes=1024,
        protocol_version=1,
    )
    return (
        ProductInspectionEvidenceReader(
            store,
            PatchArtifactReader(store, LocalArtifactCas(root / "cas")),
            verification_plan=plan,
            verification_plan_digest="4" * 64,
            promotion_target_id="integration-target",
            max_patch_chars=1024,
        ),
        session_id,
        message_id,
    )


async def _append_session(
    store: InMemoryEventStore,
    session_id: str,
    message_id: str,
    *middle: PendingEvent,
) -> None:
    await store.append(
        f"session:{session_id}",
        expected_seq=0,
        events=(
            PendingEvent(
                "session/created",
                {
                    "session_id": session_id,
                    "workspace": "inspection-workspace",
                    "metadata": {},
                },
            ),
            PendingEvent(
                "turn/start", {"turn_id": _TURN_ID, "message_id": message_id}
            ),
            *middle,
            PendingEvent(
                "turn/end",
                {
                    "turn_id": _TURN_ID,
                    "reason": "failed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
    )


def _coder(evidence):
    return next(
        node
        for node in evidence.nodes
        if node.node_id == product_role_node_id(ProductRole.CODER)
    )


async def test_failed_role_projects_latest_typed_provider_leaf(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "ProviderFailure",
                "message": "provider-response-invalid",
                "traceback": "ProviderFailure: provider-response-invalid",
                "failure_code": "provider-response-invalid",
                "failure_category": "protocol",
            },
        ),
    )

    coder = _coder(await reader.load(_summary(), None))

    assert coder.failure_code == "workflow-agent-message-failed"
    assert coder.leaf_failure_code == "provider-response-invalid"
    assert coder.leaf_failure_category == "protocol"
    assert coder.leaf_error_type is None


async def test_failed_role_projects_stable_generic_runtime_type(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "CompositionUnavailable",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )

    coder = _coder(await reader.load(_summary(), None))

    assert coder.leaf_failure_code is None
    assert coder.leaf_failure_category is None
    assert coder.leaf_error_type == "CompositionUnavailable"


async def test_failed_role_without_reliable_leaf_returns_none(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(store, session_id, message_id)

    coder = _coder(await reader.load(_summary(), None))

    assert coder.failure_code == "workflow-agent-message-failed"
    assert coder.leaf_failure_code is None
    assert coder.leaf_failure_category is None
    assert coder.leaf_error_type is None


async def test_session_invariant_violation_fails_closed(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "turn/start", {"turn_id": _TURN_ID, "message_id": message_id}
        ),
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "CompositionUnavailable",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )

    with pytest.raises(ProductStateError) as caught:
        await reader.load(_summary(), None)

    assert caught.value.code == "product-inspection-session-invalid"


async def test_malformed_typed_provider_leaf_fails_closed(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "ProviderFailure",
                "message": "provider-response-invalid",
                "traceback": "ProviderFailure: provider-response-invalid",
                "failure_code": "provider-response-invalid",
                "failure_category": "not-a-provider-category",
            },
        ),
    )

    with pytest.raises(ProductStateError) as caught:
        await reader.load(_summary(), None)

    assert caught.value.code == "product-inspection-session-invalid"


@pytest.mark.parametrize(
    ("request_id_override", "agent_preset"),
    (
        (None, "foreign-inspection-role"),
        ("foreign-create-request", "inspection-role"),
    ),
    ids=("matching-request-but-foreign-spec", "foreign-request"),
)
async def test_identity_conflict_does_not_adopt_foreign_session_leaf(
    tmp_path: Path,
    request_id_override: str | None,
    agent_preset: str,
) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(
        store,
        tmp_path,
        node_failure_code="workflow-agent-identity-conflict",
        request_id_override=request_id_override,
        agent_preset=agent_preset,
    )
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "ForeignFailure",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )

    coder = _coder(await reader.load(_summary(), None))

    assert coder.failure_code == "workflow-agent-identity-conflict"
    assert coder.agent_id is None
    assert coder.session_id is None
    assert coder.leaf_failure_code is None
    assert coder.leaf_failure_category is None
    assert coder.leaf_error_type is None


async def test_non_message_node_failure_does_not_claim_session_error(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(
        store,
        tmp_path,
        node_failure_code="workflow-artifact-capture-failed",
    )
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "EarlierTurnFailure",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )

    coder = _coder(await reader.load(_summary(), None))

    assert coder.failure_code == "workflow-artifact-capture-failed"
    assert coder.agent_id is not None
    assert coder.session_id == session_id
    assert coder.leaf_failure_code is None
    assert coder.leaf_failure_category is None
    assert coder.leaf_error_type is None


async def test_later_unrelated_turn_cannot_replace_workflow_message_leaf(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "TargetFailure",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )
    head = len(await store.read(f"session:{session_id}"))
    await store.append(
        f"session:{session_id}",
        expected_seq=head,
        events=(
            PendingEvent(
                "turn/start",
                {
                    "turn_id": "later-turn",
                    "message_id": "unrelated-later-message",
                },
            ),
            PendingEvent(
                "runtime/error",
                {
                    "turn_id": "later-turn",
                    "step_id": "later-step",
                    "error_type": "LaterFailure",
                    "message": "not used by inspection",
                    "traceback": "not used by inspection",
                },
            ),
            PendingEvent(
                "turn/end",
                {
                    "turn_id": "later-turn",
                    "reason": "failed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
    )

    coder = _coder(await reader.load(_summary(), None))

    assert coder.leaf_error_type == "TargetFailure"


async def test_later_turn_cannot_forge_target_turn_leaf(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "TargetFailure",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )
    head = len(await store.read(f"session:{session_id}"))
    await store.append(
        f"session:{session_id}",
        expected_seq=head,
        events=(
            PendingEvent(
                "turn/start",
                {
                    "turn_id": "later-turn",
                    "message_id": "unrelated-later-message",
                },
            ),
            PendingEvent(
                "runtime/error",
                {
                    "turn_id": _TURN_ID,
                    "step_id": "later-step",
                    "error_type": "LaterFailure",
                    "message": "not used by inspection",
                    "traceback": "not used by inspection",
                },
            ),
            PendingEvent(
                "turn/end",
                {
                    "turn_id": "later-turn",
                    "reason": "failed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
    )

    with pytest.raises(ProductStateError) as caught:
        await reader.load(_summary(), None)

    assert caught.value.code == "product-inspection-session-invalid"


@pytest.mark.parametrize(
    "target_tail",
    (
        (),
        (
            PendingEvent(
                "turn/end",
                {
                    "turn_id": _TURN_ID,
                    "reason": "completed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
        (
            PendingEvent(
                "runtime/error",
                {
                    "turn_id": _TURN_ID,
                    "step_id": "duplicate-step",
                    "error_type": "DuplicateFailure",
                    "message": "not used by inspection",
                    "traceback": "not used by inspection",
                },
            ),
            PendingEvent(
                "turn/end",
                {
                    "turn_id": _TURN_ID,
                    "reason": "failed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
    ),
    ids=("missing-turn-end", "completed-turn", "duplicate-runtime-error"),
)
async def test_contradictory_target_failure_chain_fails_closed(
    tmp_path: Path,
    target_tail: tuple[PendingEvent, ...],
) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await store.append(
        f"session:{session_id}",
        expected_seq=0,
        events=(
            PendingEvent(
                "session/created",
                {
                    "session_id": session_id,
                    "workspace": "inspection-workspace",
                    "metadata": {},
                },
            ),
            PendingEvent(
                "turn/start", {"turn_id": _TURN_ID, "message_id": message_id}
            ),
            PendingEvent(
                "runtime/error",
                {
                    "turn_id": _TURN_ID,
                    "step_id": _STEP_ID,
                    "error_type": "TargetFailure",
                    "message": "not used by inspection",
                    "traceback": "not used by inspection",
                },
            ),
            *target_tail,
        ),
    )

    with pytest.raises(ProductStateError) as caught:
        await reader.load(_summary(), None)

    assert caught.value.code == "product-inspection-session-invalid"


async def test_workflow_message_turn_cannot_be_replayed_with_same_identity(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    reader, session_id, message_id = await _reader(store, tmp_path)
    await _append_session(
        store,
        session_id,
        message_id,
        PendingEvent(
            "runtime/error",
            {
                "turn_id": _TURN_ID,
                "step_id": _STEP_ID,
                "error_type": "TargetFailure",
                "message": "not used by inspection",
                "traceback": "not used by inspection",
            },
        ),
    )
    head = len(await store.read(f"session:{session_id}"))
    await store.append(
        f"session:{session_id}",
        expected_seq=head,
        events=(
            PendingEvent(
                "turn/start", {"turn_id": _TURN_ID, "message_id": message_id}
            ),
            PendingEvent(
                "turn/end",
                {
                    "turn_id": _TURN_ID,
                    "reason": "failed",
                    "steps": 1,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ),
        ),
    )

    with pytest.raises(ProductStateError) as caught:
        await reader.load(_summary(), None)

    assert caught.value.code == "product-inspection-session-invalid"
