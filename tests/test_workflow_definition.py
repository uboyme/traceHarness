"""v0.7-E definition freezing, DAG rules, identities and stream strictness."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.workflow import (
    AgentTaskNode,
    ApprovalNode,
    JoinNode,
    MapNode,
    NodeStatus,
    VerificationNode,
    WorkflowDefinition,
    WorkflowNodeKind,
    WorkflowStatus,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.workflow import (
    WorkflowDefinitionError,
    WorkflowInputError,
    WorkflowProjection,
    WorkflowProtocolError,
    freeze_workflow_definition,
    workflow_definition_hash,
    workflow_stream_id,
)
from traceh.workflow.events import (
    WORKFLOW_MAP_EXPANDED,
    WORKFLOW_NODE_COMPLETED,
    WORKFLOW_NODE_STARTED,
    WORKFLOW_RUN_STARTED,
    WORKFLOW_SCHEMA_VERSION,
    map_expanded_data,
    node_completed_data,
    node_started_data,
    run_started_data,
)
from traceh.workflow.models import (
    MAX_FAN_OUT,
    agent_identity,
    freeze_map_keys,
    map_child_node_id,
    workflow_operation_id,
)

RUN = "run-1"
STREAM = workflow_stream_id(RUN)


def _task(node_id: str, predecessors: tuple[str, ...] = ()) -> AgentTaskNode:
    return AgentTaskNode(node_id, predecessors, "spec", "msg", False)


def _definition(*nodes) -> WorkflowDefinition:
    return WorkflowDefinition("demo", tuple(nodes))


# ------------------------------------------------------------------ definition


def test_a_valid_definition_freezes_and_hashes() -> None:
    definition = _definition(
        _task("a"),
        VerificationNode("v", ("a",), "a", "main-target"),
        ApprovalNode("s", ("v",), "v"),
    )
    assert freeze_workflow_definition(definition) is definition
    assert len(workflow_definition_hash(definition)) == 64


def test_the_hash_covers_every_decision_bearing_field() -> None:
    base = _definition(_task("a"), JoinNode("j", ("a",)))
    original = workflow_definition_hash(base)
    variants = (
        WorkflowDefinition("other", base.nodes),
        _definition(AgentTaskNode("a", (), "other-spec", "msg", False), JoinNode("j", ("a",))),
        _definition(AgentTaskNode("a", (), "spec", "other-msg", False), JoinNode("j", ("a",))),
        _definition(AgentTaskNode("a", (), "spec", "msg", True), JoinNode("j", ("a",))),
        _definition(_task("a"), JoinNode("k", ("a",))),
    )
    digests = {workflow_definition_hash(item) for item in variants}
    assert original not in digests
    assert len(digests) == len(variants)


def test_a_boolean_flag_is_not_an_integer() -> None:
    with pytest.raises(WorkflowInputError) as raised:
        freeze_workflow_definition(
            _definition(AgentTaskNode("a", (), "spec", "msg", 1))  # type: ignore[arg-type]
        )
    assert raised.value.code == "workflow-capture-artifact-invalid"


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        (_definition(_task("a"), _task("a")), "workflow-node-duplicate"),
        (_definition(_task("a", ("missing",))), "workflow-node-predecessor-unknown"),
        (_definition(_task("a", ("a",))), "workflow-node-self-edge"),
        (
            _definition(_task("a", ("b",)), _task("b", ("a",))),
            "workflow-definition-cycle",
        ),
        (_definition(_task("a"), _task("b", ("a", "a"))), "workflow-node-predecessor-duplicate"),
        (
            _definition(
                _task("a"),
                JoinNode("j", ("a",)),
                VerificationNode("v", ("j",), "j", "t"),
            ),
            "workflow-node-reference-invalid",
        ),
        (
            _definition(
                _task("a"),
                _task("b"),
                VerificationNode("v", ("a",), "b", "t"),
            ),
            "workflow-node-reference-unordered",
        ),
    ],
)
def test_a_broken_dag_is_refused(definition, code) -> None:
    with pytest.raises(WorkflowDefinitionError) as raised:
        freeze_workflow_definition(definition)
    assert raised.value.code == code


def test_a_cycle_behind_a_root_is_refused() -> None:
    definition = _definition(
        _task("root"),
        _task("a", ("root", "b")),
        _task("b", ("a",)),
    )
    with pytest.raises(WorkflowDefinitionError) as raised:
        freeze_workflow_definition(definition)
    assert raised.value.code == "workflow-definition-cycle"


def test_an_unreachable_node_is_refused() -> None:
    orphan = _definition(_task("a"), _task("b", ("c",)), _task("c", ("b",)))
    with pytest.raises(WorkflowDefinitionError) as raised:
        freeze_workflow_definition(orphan)
    assert raised.value.code in {
        "workflow-definition-cycle",
        "workflow-node-unreachable",
    }


def test_fan_out_is_bounded_by_the_node_and_the_protocol() -> None:
    with pytest.raises(WorkflowInputError):
        freeze_workflow_definition(
            _definition(MapNode("m", (), "k", "spec", "msg", MAX_FAN_OUT + 1, False))
        )
    assert freeze_map_keys(("b", "a"), limit=4) == ("a", "b")
    with pytest.raises(WorkflowInputError) as raised:
        freeze_map_keys(("a", "b", "c"), limit=2)
    assert raised.value.code == "workflow-map-fan-out-exceeded"
    with pytest.raises(WorkflowInputError) as raised:
        freeze_map_keys(("a", "a"), limit=4)
    assert raised.value.code == "workflow-map-keys-duplicate"
    with pytest.raises(WorkflowInputError):
        freeze_map_keys((), limit=4)


def test_hostile_containers_are_refused_not_coerced() -> None:
    class Hostile(list):
        def __eq__(self, other: object) -> bool:  # pragma: no cover - never true
            return True

        def __hash__(self) -> int:
            return 0

    with pytest.raises(WorkflowDefinitionError) as raised:
        freeze_workflow_definition(
            WorkflowDefinition("demo", Hostile([_task("a")]))  # type: ignore[arg-type]
        )
    assert raised.value.code == "workflow-definition-nodes-invalid"
    with pytest.raises(WorkflowDefinitionError):
        freeze_workflow_definition(
            _definition(AgentTaskNode("a", ["b"], "spec", "msg", False))  # type: ignore[arg-type]
        )


# -------------------------------------------------------------- identities


def test_identities_are_stable_per_run_and_node_and_never_collide() -> None:
    first = agent_identity(RUN, "a")
    assert first == agent_identity(RUN, "a")
    assert len(set(first)) == 4
    assert set(first).isdisjoint(agent_identity(RUN, "b"))
    assert set(first).isdisjoint(agent_identity("run-2", "a"))
    assert workflow_operation_id(
        run_id=RUN, node_id="a", purpose="create"
    ) != workflow_operation_id(run_id=RUN, node_id="a", purpose="message")
    assert map_child_node_id("m", "k1") != map_child_node_id("m", "k2")
    assert map_child_node_id("m", "k1") != map_child_node_id("n", "k1")


# ------------------------------------------------------------------- stream


def _envelope(seq: int, event_type: str, data, *, schema: int | None = None):
    return EventEnvelope.materialize(
        STREAM,
        seq,
        PendingEvent(
            type=event_type,
            data=data,
            schema_version=WORKFLOW_SCHEMA_VERSION if schema is None else schema,
        ),
    )


def _started(digest: str = "a" * 64):
    return _envelope(
        1,
        WORKFLOW_RUN_STARTED,
        run_started_data(run_id=RUN, definition_id="demo", definition_hash=digest),
    )


def test_a_complete_stream_rebuilds_the_run() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="a",
                kind=WorkflowNodeKind.AGENT_TASK,
                map_key=None,
                agent_id=agent_identity(RUN, "a")[0],
                message_id=agent_identity(RUN, "a")[3],
            ),
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    assert projection.head_seq == 3
    assert projection.status_of("a") is NodeStatus.COMPLETED
    assert projection.state("a").agent_id == agent_identity(RUN, "a")[0]


def _run_start(seq: int):
    return _envelope(
        seq,
        WORKFLOW_RUN_STARTED,
        run_started_data(run_id=RUN, definition_id="demo", definition_hash="a" * 64),
    )


def _join_started(seq: int):
    return _envelope(
        seq,
        WORKFLOW_NODE_STARTED,
        node_started_data(node_id="a", kind=WorkflowNodeKind.JOIN, map_key=None),
    )


def _join_completed(seq: int):
    return _envelope(
        seq,
        WORKFLOW_NODE_COMPLETED,
        node_completed_data(node_id="a", kind=WorkflowNodeKind.JOIN, map_key=None),
    )


@pytest.mark.parametrize(
    ("events", "code"),
    [
        ((_run_start(2),), "workflow-sequence-invalid"),
        ((_started(), _run_start(2)), "workflow-run-start-unexpected"),
        ((_join_started(1),), "workflow-run-start-missing"),
        ((_started(), _join_completed(2)), "workflow-node-terminal-unexpected"),
    ],
)
def test_a_broken_stream_is_refused(events, code) -> None:
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == code


def test_an_unknown_schema_or_event_type_is_refused() -> None:
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(
            RUN,
            (
                _envelope(
                    1,
                    WORKFLOW_RUN_STARTED,
                    run_started_data(
                        run_id=RUN, definition_id="demo", definition_hash="a" * 64
                    ),
                    schema=2,
                ),
            ),
        )
    assert raised.value.code == "workflow-schema-version-unsupported"

    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(
            RUN, (_envelope(1, "workflow/node-retried", {"node_id": "a"}),)
        )
    assert raised.value.code == "workflow-event-type-unknown"


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_unexpected_payload_keys_are_refused(mutation: str) -> None:
    data = dict(
        run_started_data(run_id=RUN, definition_id="demo", definition_hash="a" * 64)
    )
    if mutation == "extra":
        data["workspace_path"] = "/somewhere"
    else:
        data.pop("definition_id")
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, (_envelope(1, WORKFLOW_RUN_STARTED, data),))
    assert raised.value.code == "workflow-payload-keys-unexpected"


def test_a_forged_map_child_identity_is_recomputed_and_refused() -> None:
    data = dict(map_expanded_data(node_id="m", map_keys=("k1",)))
    data["child_node_ids"] = ["wf-child-forged"]
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(3, WORKFLOW_MAP_EXPANDED, data),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-map-child-id-invalid"


def test_a_hostile_envelope_becomes_a_stable_protocol_error() -> None:
    class Hostile:
        @property
        def stream_id(self) -> str:
            raise RuntimeError("the envelope refuses to be read")

        seq = 1

    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, (Hostile(),))  # type: ignore[arg-type]
    assert raised.value.code == "workflow-payload-invalid"


def test_a_hostile_envelope_never_swallows_base_exceptions() -> None:
    class Interrupting:
        @property
        def stream_id(self) -> str:
            raise KeyboardInterrupt

        seq = 1

    with pytest.raises(KeyboardInterrupt):
        WorkflowProjection.rebuild(RUN, (Interrupting(),))  # type: ignore[arg-type]


def test_a_replayed_run_is_independent_of_the_envelope_timestamp() -> None:
    event = _started()
    later = dataclasses.replace(
        event, occurred_at=event.occurred_at + timedelta(seconds=5)
    )
    assert (
        WorkflowProjection.rebuild(RUN, (event,)).definition_hash
        == WorkflowProjection.rebuild(RUN, (later,)).definition_hash
    )


async def test_an_empty_stream_is_a_run_that_has_not_started() -> None:
    from traceh.workflow import WorkflowStreamReader

    projection = await WorkflowStreamReader(InMemoryEventStore()).load(RUN)
    assert projection.head_seq == 0
    assert projection.running_nodes() == ()


def test_a_terminal_fact_cannot_redefine_the_node_it_ends() -> None:
    """A started AgentTask may not finish as a Join holding a foreign Artifact."""

    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="a",
                kind=WorkflowNodeKind.JOIN,
                map_key=None,
                artifact_id="patch-" + "7" * 64,
            ),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-terminal-mismatch"


def test_a_terminal_fact_cannot_invent_a_map_key() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="m", kind=WorkflowNodeKind.MAP, map_key="alpha"
            ),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-terminal-mismatch"


def test_a_run_cannot_report_nodes_the_definition_never_declared() -> None:
    """Internal consistency is not enough; the definition must declare it."""

    events = (
        _started(digest=workflow_definition_hash(_definition(_task("a")))),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="ghost", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="ghost",
                kind=WorkflowNodeKind.AGENT_TASK,
                map_key=None,
                agent_id=agent_identity(RUN, "ghost")[0],
                message_id=agent_identity(RUN, "ghost")[3],
            ),
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(_definition(_task("a")))
    assert raised.value.code == "workflow-node-undeclared"


def test_a_run_rejects_a_node_whose_kind_the_definition_contradicts() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="a", kind=WorkflowNodeKind.JOIN, map_key=None),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(node_id="a", kind=WorkflowNodeKind.JOIN, map_key=None),
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(_definition(_task("a")))
    assert raised.value.code == "workflow-node-kind-mismatch"


def _agent_completed(seq: int, node_id: str, kind=WorkflowNodeKind.AGENT_TASK, **over):
    fields = {
        "agent_id": agent_identity(RUN, node_id)[0],
        "message_id": agent_identity(RUN, node_id)[3],
    }
    fields.update(over)
    return _envelope(
        seq,
        WORKFLOW_NODE_COMPLETED,
        node_completed_data(node_id=node_id, kind=kind, map_key=None, **fields),
    )


def test_a_completion_cannot_name_an_agent_this_node_never_addressed() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _agent_completed(3, "a", agent_id=agent_identity("other-run", "a")[0]),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-result-identity-invalid"


def test_an_agent_task_cannot_complete_without_its_agent_evidence() -> None:
    """A foreign Artifact with no Agent or message is not a completed task."""

    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="a",
                kind=WorkflowNodeKind.AGENT_TASK,
                map_key=None,
                artifact_id="patch-" + "8" * 64,
            ),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-result-identity-invalid"


def test_a_join_cannot_complete_holding_evidence_it_never_produced() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="j", kind=WorkflowNodeKind.JOIN, map_key=None),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="j",
                kind=WorkflowNodeKind.JOIN,
                map_key=None,
                review_id="review-" + "3" * 32,
            ),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-result-unexpected"


def test_a_verification_cannot_complete_without_its_review() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="v", kind=WorkflowNodeKind.VERIFICATION, map_key=None
            ),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="v",
                kind=WorkflowNodeKind.VERIFICATION,
                map_key=None,
                artifact_id="patch-" + "5" * 64,
            ),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-result-incomplete"


def test_a_run_cannot_report_completed_while_a_node_never_ran() -> None:
    """`run-finished(completed)` may not stand in for the DAG itself."""

    events = (
        _started(digest=workflow_definition_hash(_definition(_task("a"), _task("b")))),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(
                node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
            ),
        ),
        _agent_completed(3, "a"),
        _envelope(
            4,
            "workflow/run-finished",
            {"status": "completed", "failure_code": None},
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(_definition(_task("a"), _task("b")))
    assert raised.value.code == "workflow-run-completion-incomplete"
    assert raised.value.node_id == "b"


def test_an_empty_run_cannot_declare_itself_completed() -> None:
    events = (
        _started(digest=workflow_definition_hash(_definition(_task("a")))),
        _envelope(
            2, "workflow/run-finished", {"status": "completed", "failure_code": None}
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(_definition(_task("a")))
    assert raised.value.code == "workflow-run-completion-incomplete"


def _completed_stream(node_id, kind, digest, **evidence):
    return (
        _started(digest=digest),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=node_id, kind=kind, map_key=None),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id=node_id, kind=kind, map_key=None, **evidence
            ),
        ),
    )


def test_a_task_that_captures_nothing_cannot_hold_an_artifact() -> None:
    """`capture_artifact=False` means this node produced no Artifact at all."""

    plan = _definition(AgentTaskNode("a", (), "spec", "msg", False))
    events = _completed_stream(
        "a",
        WorkflowNodeKind.AGENT_TASK,
        workflow_definition_hash(plan),
        agent_id=agent_identity(RUN, "a")[0],
        message_id=agent_identity(RUN, "a")[3],
        artifact_id="patch-" + "4" * 64,
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(plan)
    assert raised.value.code == "workflow-node-result-unexpected"
    assert raised.value.node_id == "a"


def test_a_task_asked_to_capture_must_produce_an_artifact() -> None:
    plan = _definition(AgentTaskNode("a", (), "spec", "msg", True))
    events = _completed_stream(
        "a",
        WorkflowNodeKind.AGENT_TASK,
        workflow_definition_hash(plan),
        agent_id=agent_identity(RUN, "a")[0],
        message_id=agent_identity(RUN, "a")[3],
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(plan)
    assert raised.value.code == "workflow-node-result-incomplete"
    assert raised.value.node_id == "a"


def test_a_map_parent_cannot_hold_an_approval_digest() -> None:
    """A Map parent completes on its expansion; it approves nothing."""

    plan = _definition(MapNode("m", (), "keys", "spec", "msg", 4, False))
    events = _completed_stream(
        "m",
        WorkflowNodeKind.MAP,
        workflow_definition_hash(plan),
        approval_digest="f" * 64,
    )
    # Kind alone settles this, so the public Projector refuses it on replay.
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-result-unexpected"
    assert raised.value.node_id == "m"


def test_a_map_child_follows_its_parents_capture_setting() -> None:
    plan = _definition(MapNode("m", (), "keys", "spec", "msg", 4, True))
    child = map_child_node_id("m", "alpha")
    events = (
        _started(digest=workflow_definition_hash(plan)),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))),
        _envelope(
            4,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            5,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=child, kind=WorkflowNodeKind.MAP, map_key="alpha"),
        ),
        _envelope(
            6,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id=child,
                kind=WorkflowNodeKind.MAP,
                map_key="alpha",
                agent_id=agent_identity(RUN, child)[0],
                message_id=agent_identity(RUN, child)[3],
            ),
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    # The parent asked for capture, so a child completing without one is invalid.
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(plan)
    assert raised.value.code == "workflow-node-result-incomplete"
    assert raised.value.node_id == child


def test_the_public_projector_fails_closed_without_any_definition() -> None:
    """Replay must reject kind-decidable malformed terminals on its own.

    `rebuild()` and `WorkflowStreamReader.load()` are public and take no
    definition, so the base layer has to stand alone. Deferring everything to
    `run(definition)` would let a caller that only replays observe a Join
    holding a Review, or an AgentTask with no Agent at all.
    """

    cases = (
        (WorkflowNodeKind.JOIN, {"review_id": "review-" + "9" * 32}),
        (WorkflowNodeKind.JOIN, {"artifact_id": "patch-" + "9" * 64}),
        (WorkflowNodeKind.AGENT_TASK, {}),
        (WorkflowNodeKind.VERIFICATION, {}),
        (
            WorkflowNodeKind.APPROVAL,
            {"artifact_id": "patch-" + "1" * 64, "review_id": "review-" + "2" * 32},
        ),
        (WorkflowNodeKind.MAP, {"approval_digest": "f" * 64}),
    )
    for kind, evidence in cases:
        events = (
            _started(),
            _envelope(
                2,
                WORKFLOW_NODE_STARTED,
                node_started_data(node_id="n", kind=kind, map_key=None),
            ),
            _envelope(
                3,
                WORKFLOW_NODE_COMPLETED,
                node_completed_data(
                    node_id="n", kind=kind, map_key=None, **evidence
                ),
            ),
        )
        with pytest.raises(WorkflowProtocolError) as raised:
            WorkflowProjection.rebuild(RUN, events)
        assert raised.value.code.startswith("workflow-node-result-"), (kind, evidence)


def test_the_base_layer_leaves_capture_artifact_to_the_definition() -> None:
    """The two layers are complementary, not duplicates.

    A stream cannot say whether a task was asked to capture, so replay must
    accept both shapes and let `run(definition)` decide.
    """

    for evidence in ({}, {"artifact_id": "patch-" + "3" * 64}):
        events = (
            _started(),
            _envelope(
                2,
                WORKFLOW_NODE_STARTED,
                node_started_data(
                    node_id="a", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
                ),
            ),
            _envelope(
                3,
                WORKFLOW_NODE_COMPLETED,
                node_completed_data(
                    node_id="a",
                    kind=WorkflowNodeKind.AGENT_TASK,
                    map_key=None,
                    agent_id=agent_identity(RUN, "a")[0],
                    message_id=agent_identity(RUN, "a")[3],
                    **evidence,
                ),
            ),
        )
        projection = WorkflowProjection.rebuild(RUN, events)
        assert projection.status_of("a") is NodeStatus.COMPLETED
        # Whichever shape it is, exactly one definition accepts it.
        captures = _definition(AgentTaskNode("a", (), "spec", "msg", True))
        does_not = _definition(AgentTaskNode("a", (), "spec", "msg", False))
        accepted = []
        for plan in (captures, does_not):
            try:
                projection.run(plan)
                accepted.append(plan)
            except WorkflowProtocolError:
                pass
        assert len(accepted) == 1, evidence


def _map_plan(capture: bool = False) -> WorkflowDefinition:
    return _definition(MapNode("m", (), "keys", "spec", "msg", 4, capture))


def test_a_declared_node_may_not_claim_a_map_key() -> None:
    """A Map parent that records a key would read as its own child.

    That is what lets it carry Agent evidence it never produced: the evidence
    rule would infer "child" from the key while the definition says "parent".
    """

    plan = _map_plan()
    events = (
        _started(digest=workflow_definition_hash(plan)),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key="alpha"),
        ),
        _envelope(
            3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
        _envelope(
            4,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id="m",
                kind=WorkflowNodeKind.MAP,
                map_key="alpha",
                agent_id=agent_identity(RUN, "m")[0],
                message_id=agent_identity(RUN, "m")[3],
            ),
        ),
    )
    # Replay settles this on its own: nothing expanded "m", so it has no key.
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-map-key-invalid"


def test_a_map_child_must_record_the_key_its_identity_came_from() -> None:
    plan = _map_plan()
    child = map_child_node_id("m", "alpha")
    events = (
        _started(digest=workflow_definition_hash(plan)),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
        _envelope(
            4,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            5,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=child, kind=WorkflowNodeKind.MAP, map_key="beta"),
        ),
        _envelope(
            6,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id=child,
                kind=WorkflowNodeKind.MAP,
                map_key="beta",
                agent_id=agent_identity(RUN, child)[0],
                message_id=agent_identity(RUN, child)[3],
            ),
        ),
    )
    # The expansion derived this id from "alpha", so "beta" is not its key.
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-map-key-invalid"


def test_a_matching_map_key_is_accepted() -> None:
    """The positive case, so the rule cannot be satisfied by refusing everything."""

    plan = _map_plan()
    child = map_child_node_id("m", "alpha")
    events = (
        _started(digest=workflow_definition_hash(plan)),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
        _envelope(
            4,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            5,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=child, kind=WorkflowNodeKind.MAP, map_key="alpha"),
        ),
        _envelope(
            6,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(
                node_id=child,
                kind=WorkflowNodeKind.MAP,
                map_key="alpha",
                agent_id=agent_identity(RUN, child)[0],
                message_id=agent_identity(RUN, child)[3],
            ),
        ),
        _envelope(
            7, "workflow/run-finished", {"status": "completed", "failure_code": None}
        ),
    )
    run = WorkflowProjection.rebuild(RUN, events).run(plan)
    assert run.status is WorkflowStatus.COMPLETED
    assert run.outcome(child).status is NodeStatus.COMPLETED
    # The parent completes on its expansion and runs no Agent of its own.
    assert run.outcome("m").agent_id is None


def test_only_a_running_map_parent_may_record_an_expansion() -> None:
    """A stream alone must refuse a fan-out from a node that cannot fan out."""

    for kind in (
        WorkflowNodeKind.JOIN,
        WorkflowNodeKind.AGENT_TASK,
        WorkflowNodeKind.VERIFICATION,
        WorkflowNodeKind.APPROVAL,
    ):
        events = (
            _started(),
            _envelope(
                2,
                WORKFLOW_NODE_STARTED,
                node_started_data(node_id="n", kind=kind, map_key=None),
            ),
            _envelope(
                3,
                WORKFLOW_MAP_EXPANDED,
                map_expanded_data(node_id="n", map_keys=("alpha",)),
            ),
        )
        with pytest.raises(WorkflowProtocolError) as raised:
            WorkflowProjection.rebuild(RUN, events)
        assert raised.value.code == "workflow-map-expansion-unexpected", kind


def test_a_node_nobody_expanded_may_not_claim_a_map_key() -> None:
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key="alpha"),
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-node-map-key-invalid"


def test_one_child_belongs_to_exactly_one_expansion() -> None:
    """Two Maps cannot both claim the same child, nor can one claim it twice."""

    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
        _envelope(
            4, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-map-expansion-duplicate"


def test_a_child_cannot_start_before_the_expansion_that_created_it() -> None:
    child = map_child_node_id("m", "alpha")
    events = (
        _started(),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=child, kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            4, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
    )
    with pytest.raises(WorkflowProtocolError) as raised:
        WorkflowProjection.rebuild(RUN, events)
    assert raised.value.code == "workflow-map-child-duplicate"


def test_the_definition_layer_still_binds_a_key_replay_accepted() -> None:
    """The two layers remain complementary for map keys as well.

    Replay can only compare a key against the expansion it saw. If a definition
    separately *declares* a node whose id happens to be a real child id, that
    node is a declared node and may not carry a key - and only the definition
    can say so.
    """

    child = map_child_node_id("m", "alpha")
    plan = _definition(
        MapNode("m", (), "keys", "spec", "msg", 4, False),
        AgentTaskNode(child, ("m",), "spec", "msg", False),
    )
    events = (
        _started(digest=workflow_definition_hash(plan)),
        _envelope(
            2,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            3, WORKFLOW_MAP_EXPANDED, map_expanded_data(node_id="m", map_keys=("alpha",))
        ),
        _envelope(
            4,
            WORKFLOW_NODE_COMPLETED,
            node_completed_data(node_id="m", kind=WorkflowNodeKind.MAP, map_key=None),
        ),
        _envelope(
            5,
            WORKFLOW_NODE_STARTED,
            node_started_data(node_id=child, kind=WorkflowNodeKind.MAP, map_key="alpha"),
        ),
    )
    projection = WorkflowProjection.rebuild(RUN, events)
    with pytest.raises(WorkflowProtocolError) as raised:
        projection.run(plan)
    assert raised.value.code == "workflow-node-map-key-invalid"
    assert raised.value.node_id == child
