"""Pure durable-fact tests for the TUI ProductTask conversation projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    AGENT_EVENT_SCHEMA_VERSION,
    agent_created_data,
)
from traceh.api.agents import AgentSpec
from traceh.api.events import PendingEvent
from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelMessage, ModelRequest
from traceh.api.product import (
    ProductRole,
    ProductTaskStatus,
    ProductTaskSummary,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.product.errors import ProductStateError
from traceh.product.execution import product_task_owner_id
from traceh.product.inspection import ProductNodeEvidence, ProductTaskEvidence
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
)
from traceh.product.topology import product_role_node_id
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tui.task_conversation import TaskConversationReader
from traceh.workflow.models import agent_identity

TASK_ID = "task-conversation-projection"
ROUTER_AGENT_ID = "router-agent-conversation"
ROUTER_SESSION_ID = "router-session-conversation"
BASE_TIME = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)


class StrictReadStore(InMemoryEventStore):
    """Count writes and make accidental whole-store discovery fail loudly."""

    def __init__(self) -> None:
        super().__init__()
        self.append_calls = 0
        self.list_stream_calls = 0

    async def append(
        self,
        stream_id,
        *,
        expected_seq,
        events,
        durability=Durability.SYNC,
    ):
        self.append_calls += 1
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )

    async def list_streams(self, *, prefix=None):
        del prefix
        self.list_stream_calls += 1
        raise AssertionError("conversation projection must not scan Store streams")


@dataclass(frozen=True, slots=True)
class ConversationFixture:
    store: StrictReadStore
    observation: ProductObservation
    sessions: dict[str, str]


def _request(
    session_id: str,
    turn_id: str,
    step_id: str,
    revision: str,
    content: str,
) -> ModelRequest:
    return ModelRequest(
        provider="deterministic-provider",
        model="deterministic-model",
        messages=(ModelMessage(role="user", content=content),),
        metadata={
            "session_id": session_id,
            "turn_id": turn_id,
            "step_id": step_id,
            "composition_revision": revision,
        },
    )


def _turn_events(
    session_id: str,
    *,
    suffix: str,
    base_seq: int,
    input_text: str,
    model_text: str,
    usage_quality: str = "exact",
    with_shell: bool = False,
    with_list_files: bool = False,
    with_search_text: bool = False,
    shell_exit_code: int | None = None,
    shell_status: str = "succeeded",
) -> tuple[PendingEvent, ...]:
    turn_id = f"turn-{suffix}"
    step_id = f"step-{suffix}"
    attempt_id = f"attempt-{suffix}"
    revision = f"revision-{suffix}"
    source_seq = base_seq + 3
    snapshot_seq = base_seq + 4
    request = _request(session_id, turn_id, step_id, revision, input_text)
    request_data = request.to_dict()
    request_digest = fingerprint(request_data)
    tool_calls = []
    if with_shell:
        tool_calls.append(
            {
                "id": f"call-{suffix}",
                "name": "shell",
                "arguments": {
                    "command": "echo SECRET-IN-ARGUMENT\x1b[31m\u202e"
                },
            }
        )
    if with_list_files:
        tool_calls.append(
            {
                "id": f"call-list-files-{suffix}",
                "name": "list_files",
                "arguments": {},
            }
        )
    if with_search_text:
        tool_calls.append(
            {
                "id": f"call-search-text-{suffix}",
                "name": "search_text",
                "arguments": {"query": "reservation_handler", "path": "src"},
            }
        )
    events = [
        PendingEvent(
            "turn/start",
            {"turn_id": turn_id, "message_id": f"message-{suffix}"},
        ),
        PendingEvent(
            "step/start",
            {"turn_id": turn_id, "step_id": step_id, "number": 1},
        ),
        PendingEvent(
            "user/message",
            {"turn_id": turn_id, "step_id": step_id, "content": input_text},
        ),
        PendingEvent(
            "request/snapshot",
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "source_seq": source_seq,
                "composition_revision": revision,
                "composed_fingerprint": request_digest,
                "dispatch_fingerprint": request_digest,
                "composed_request": request_data,
                "dispatch_request": request_data,
            },
            composition_revision=revision,
        ),
        PendingEvent(
            "model/attempt-start",
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "ordinal": 1,
                "request_snapshot_seq": snapshot_seq,
                "dispatch_fingerprint": request_digest,
                "reservation_id": None,
                "provider": "deterministic-provider",
                "model": "deterministic-model",
                "retry_wait_milliseconds": 0,
                "retry_failure_code": None,
                "retry_failure_category": None,
            },
        ),
        PendingEvent(
            "assistant/message",
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "content": model_text,
                "tool_calls": tool_calls,
            },
        ),
        PendingEvent(
            "model/attempt-end",
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "ordinal": 1,
                "request_snapshot_seq": snapshot_seq,
                "dispatch_fingerprint": request_digest,
                "reservation_id": None,
                "status": "succeeded",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 5,
                    "total_tokens": 12,
                    "quality": usage_quality,
                },
            },
        ),
    ]
    if with_shell:
        result_data = (
            {}
            if shell_exit_code is None
            else {"data": {"exit_code": shell_exit_code}}
        )
        events.extend(
            (
                PendingEvent(
                    "tool/call",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-{suffix}",
                        "tool_name": "shell",
                        "arguments": {
                            "command": "echo SECRET-IN-ARGUMENT\x1b[31m\u202e"
                        },
                    },
                ),
                PendingEvent(
                    "tool/result",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-{suffix}",
                        "tool_name": "shell",
                        "status": shell_status,
                        "content": "SECRET-IN-RESULT\x1b[32m\u2028\u202e",
                        **result_data,
                    },
                ),
            )
        )
    if with_list_files:
        events.extend(
            (
                PendingEvent(
                    "tool/call",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-list-files-{suffix}",
                        "tool_name": "list_files",
                        "arguments": {},
                    },
                ),
                PendingEvent(
                    "tool/result",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-list-files-{suffix}",
                        "tool_name": "list_files",
                        "status": "succeeded",
                        "content": "[]",
                    },
                ),
            )
        )
    if with_search_text:
        events.extend(
            (
                PendingEvent(
                    "tool/call",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-search-text-{suffix}",
                        "tool_name": "search_text",
                        "arguments": {
                            "query": "reservation_handler",
                            "path": "src",
                        },
                    },
                ),
                PendingEvent(
                    "tool/result",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "tool_call_id": f"call-search-text-{suffix}",
                        "tool_name": "search_text",
                        "status": "succeeded",
                        "content": "src/module.py:1: reservation_handler",
                    },
                ),
            )
        )
    events.extend(
        (
            PendingEvent("step/end", {"turn_id": turn_id, "step_id": step_id}),
            PendingEvent("turn/end", {"turn_id": turn_id, "reason": "completed"}),
        )
    )
    return tuple(
        replace(event, occurred_at=BASE_TIME + timedelta(seconds=base_seq + index))
        for index, event in enumerate(events, start=1)
    )


async def _append_agent(
    store: StrictReadStore,
    *,
    agent_id: str,
    session_id: str,
    request_id: str,
    role: str,
    owner_agent_id: str | None = None,
) -> None:
    head = await store.head(AGENT_DIRECTORY_STREAM)
    await store.append(
        AGENT_DIRECTORY_STREAM,
        expected_seq=head,
        events=(
            PendingEvent(
                AGENT_CREATED,
                agent_created_data(
                    agent_id=agent_id,
                    session_id=session_id,
                    request_id=request_id,
                    spec=AgentSpec(
                        preset=role,
                        workspace_id=f"workspace-{role}",
                        owner_agent_id=owner_agent_id,
                    ),
                ),
                schema_version=AGENT_EVENT_SCHEMA_VERSION,
            ),
        ),
    )


async def _append_session(
    store: StrictReadStore,
    session_id: str,
    *,
    role: str,
    usage_quality: str = "exact",
    with_shell: bool = False,
    with_list_files: bool = False,
    with_search_text: bool = False,
    shell_exit_code: int | None = None,
    shell_status: str = "succeeded",
) -> None:
    stream = SessionService.session_stream(session_id)
    await store.append(
        stream,
        expected_seq=0,
        events=(
            PendingEvent(
                "session/created",
                {
                    "session_id": session_id,
                    "workspace": f"workspace-{role}",
                    "metadata": {},
                },
                occurred_at=BASE_TIME,
            ),
        ),
    )
    await store.append(
        stream,
        expected_seq=1,
        events=_turn_events(
            session_id,
            suffix=role,
            base_seq=1,
            input_text=f"input-for-{role}",
            model_text=f"model-output-for-{role}",
            usage_quality=usage_quality,
            with_shell=with_shell,
            with_list_files=with_list_files,
            with_search_text=with_search_text,
            shell_exit_code=shell_exit_code,
            shell_status=shell_status,
        ),
    )


async def _build_fixture(
    *,
    coder_usage_quality: str = "exact",
    coder_shell: bool = False,
    coder_list_files: bool = False,
    coder_search_text: bool = False,
    coder_shell_exit_code: int | None = None,
    coder_shell_status: str = "succeeded",
    foreign_owner_role: str | None = None,
) -> ConversationFixture:
    store = StrictReadStore()
    owner_id = product_task_owner_id(TASK_ID)
    await _append_agent(
        store,
        agent_id=owner_id,
        session_id=f"product-owner-session-{TASK_ID}",
        request_id=f"product-owner-create-{TASK_ID}",
        role="traceh-product-owner",
    )
    foreign_owner_id = "foreign-product-owner"
    if foreign_owner_role is not None:
        await _append_agent(
            store,
            agent_id=foreign_owner_id,
            session_id="foreign-product-owner-session",
            request_id="foreign-product-owner-create",
            role="traceh-product-owner",
        )
    identities = {
        "router": (ROUTER_AGENT_ID, ROUTER_SESSION_ID, "router-request"),
    }
    for role in ProductRole:
        node_id = product_role_node_id(role)
        agent_id, session_id, request_id, _ = agent_identity(TASK_ID, node_id)
        identities[role.value] = (agent_id, session_id, request_id)

    for role, (agent_id, session_id, request_id) in identities.items():
        await _append_agent(
            store,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            role=role,
            owner_agent_id=(
                foreign_owner_id if role == foreign_owner_role else owner_id
            ),
        )
        await _append_session(
            store,
            session_id,
            role=role,
            usage_quality=(
                coder_usage_quality if role == ProductRole.CODER.value else "exact"
            ),
            with_shell=coder_shell and role == ProductRole.CODER.value,
            with_list_files=(
                coder_list_files and role == ProductRole.CODER.value
            ),
            with_search_text=(
                coder_search_text and role == ProductRole.CODER.value
            ),
            shell_exit_code=(
                coder_shell_exit_code
                if role == ProductRole.CODER.value
                else None
            ),
            shell_status=(
                coder_shell_status
                if role == ProductRole.CODER.value
                else "succeeded"
            ),
        )

    summary = ProductTaskSummary(
        task_id=TASK_ID,
        status=ProductTaskStatus.COMPLETED,
        requested_mode=RequestedTaskMode.AUTO,
        mode_source=TaskModeSource.CONFIRMED_PROPOSAL,
        requirement_digest="1" * 64,
        profile_digest="2" * 64,
        preflight_digest="3" * 64,
        origin_session_id="origin-session",
        origin_turn_id="origin-turn",
        origin_message_id="origin-message",
        confirmation_session_id="confirmation-session",
        confirmation_turn_id="confirmation-turn",
        confirmation_message_id="confirmation-message",
        head_seq=6,
        resolved_mode=ResolvedTaskMode.MULTI,
        router_agent_id=ROUTER_AGENT_ID,
        routing_session_id=ROUTER_SESSION_ID,
        definition_hash="4" * 64,
        assembly_digest="5" * 64,
        source_base_revision="6" * 40,
    )
    nodes = []
    for role in ProductRole:
        node_id = product_role_node_id(role)
        agent_id, session_id, _, _ = agent_identity(TASK_ID, node_id)
        nodes.append(
            ProductNodeEvidence(
                node_id=node_id,
                kind="agent_task",
                status="completed",
                agent_id=agent_id,
                session_id=session_id,
                failure_code=None,
            )
        )
    evidence = ProductTaskEvidence(
        workflow_status="completed",
        nodes=tuple(nodes),
        review=None,
    )
    session_ids = {
        role: session_id for role, (_agent, session_id, _request) in identities.items()
    }
    heads = []
    for session_id in session_ids.values():
        events = await store.read(SessionService.session_stream(session_id))
        latest = events[-1]
        heads.append(
            ObservedStreamHead(
                stream_id=latest.stream_id,
                seq=latest.seq,
                event_type=latest.type,
                occurred_at=latest.occurred_at,
                task_bound=True,
            )
        )
    observation = ProductObservation(
        task_id=TASK_ID,
        summary=summary,
        workflow=None,
        evidence=evidence,
        review=None,
        approval=None,
        promotion=None,
        approval_digest=None,
        stream_heads=tuple(heads),
        observed_at=BASE_TIME + timedelta(minutes=5),
    )
    return ConversationFixture(store, observation, session_ids)


async def _heads(store: StrictReadStore, session_ids: dict[str, str]) -> dict[str, int]:
    streams = [AGENT_DIRECTORY_STREAM]
    for session_id in session_ids.values():
        streams.extend(
            (
                SessionService.session_stream(session_id),
                SessionService.effect_stream(session_id),
            )
        )
    return {stream: await store.head(stream) for stream in streams}


async def _append_coder_turn(
    fixture: ConversationFixture,
    *,
    suffix: str,
    input_text: str,
    model_text: str,
) -> None:
    session_id = fixture.sessions["coder"]
    stream = SessionService.session_stream(session_id)
    head = await fixture.store.head(stream)
    await fixture.store.append(
        stream,
        expected_seq=head,
        events=_turn_events(
            session_id,
            suffix=suffix,
            base_seq=head,
            input_text=input_text,
            model_text=model_text,
        ),
    )


async def test_projects_exact_router_and_fixed_multi_roles() -> None:
    fixture = await _build_fixture()

    snapshot = await TaskConversationReader(fixture.store).load(
        fixture.observation,
        observed_at=BASE_TIME + timedelta(minutes=5),
    )

    assert [role.role for role in snapshot.roles] == [
        "router",
        "parent",
        "reviewer",
        "coder",
    ]
    assert all(role.turns_started == role.turns_completed == 1 for role in snapshot.roles)
    assert all(role.usage_tokens == 12 for role in snapshot.roles)
    assert all(role.usage_quality == "exact" for role in snapshot.roles)
    for role in snapshot.roles:
        assert [kind for kind, _content in role.messages] == ["input", "model"]
        assert role.messages[-1][1] == f"model-output-for-{role.role}"
    assert fixture.store.list_stream_calls == 0


async def test_user_and_assistant_messages_have_no_projection_size_caps() -> None:
    fixture = await _build_fixture()
    input_lines = [
        f"release-readiness item {number}: preserve the recorded decision"
        for number in range(64)
    ]
    final_line = "FINAL-RELEASE-DECISION: retain the complete durable explanation"
    input_text = "\n".join((*input_lines, final_line))
    model_text = (
        "Dependency audit result: "
        + "verified-without-a-regression;" * 350
        + " all requested checks completed."
    )
    assert len(input_text.split("\n")) > 40
    assert len(model_text) > 4_000

    await _append_coder_turn(
        fixture,
        suffix="complete-large-messages",
        input_text=input_text,
        model_text=model_text,
    )

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")

    assert coder.messages[-2:] == (
        ("input", input_text),
        ("model", model_text),
    )
    assert coder.messages[-2][1].split("\n")[-1] == final_line
    assert "more lines omitted" not in coder.messages[-2][1]
    assert not coder.messages[-1][1].endswith("…")


async def test_complete_messages_escape_unsafe_characters_without_truncation() -> None:
    fixture = await _build_fixture()
    input_text = "review\x1b[31m\rstatus\x00\u2028paragraph\u2029override\u202eend"
    model_text = "result-before\x1b[32m\u202e-result-after"

    await _append_coder_turn(
        fixture,
        suffix="complete-sanitized-messages",
        input_text=input_text,
        model_text=model_text,
    )

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")

    assert coder.messages[-2:] == (
        (
            "input",
            "review\\x1b[31m\\rstatus\\0\\u2028paragraph"
            "\\u2029override\\u202eend",
        ),
        ("model", "result-before\\x1b[32m\\u202e-result-after"),
    )
    rendered = "\n".join(content for _kind, content in coder.messages[-2:])
    for unsafe in ("\x1b", "\r", "\x00", "\u2028", "\u2029", "\u202e"):
        assert unsafe not in rendered


async def test_router_session_must_belong_to_the_recorded_router_agent() -> None:
    fixture = await _build_fixture()
    assert fixture.observation.summary is not None
    unrelated_session = "session-unrelated-router-claim"
    await _append_session(fixture.store, unrelated_session, role="unrelated")
    unrelated_head = await fixture.store.head(
        SessionService.session_stream(unrelated_session)
    )
    tampered = replace(
        fixture.observation,
        summary=replace(
            fixture.observation.summary,
            routing_session_id=unrelated_session,
        ),
        stream_heads=fixture.observation.stream_heads
        + (
            ObservedStreamHead(
                SessionService.session_stream(unrelated_session),
                unrelated_head,
                "turn/end",
                BASE_TIME + timedelta(seconds=11),
                True,
            ),
        ),
    )

    with pytest.raises(ProductStateError) as raised:
        await TaskConversationReader(fixture.store).load(tampered)

    assert raised.value.code == "product-conversation-router-session-mismatch"


@pytest.mark.parametrize("role", ("router", "coder"))
async def test_projected_agents_must_belong_to_the_product_owner_subtree(
    role: str,
) -> None:
    fixture = await _build_fixture(foreign_owner_role=role)

    with pytest.raises(ProductStateError) as raised:
        await TaskConversationReader(fixture.store).load(fixture.observation)

    assert raised.value.code == "product-conversation-agent-owner-mismatch"


async def test_invalid_session_lifecycle_is_rejected_before_surface_projection() -> None:
    fixture = await _build_fixture(coder_shell=True)
    coder_session = fixture.sessions["coder"]
    stream = SessionService.session_stream(coder_session)
    head = await fixture.store.head(stream)
    await fixture.store.append(
        stream,
        expected_seq=head,
        events=(
            PendingEvent(
                "tool/result",
                {
                    "turn_id": "turn-coder",
                    "step_id": "step-coder",
                    "tool_call_id": "call-coder",
                    "tool_name": "shell",
                    "status": "succeeded",
                    "content": "forged-late-result",
                },
            ),
        ),
    )

    with pytest.raises(ProductStateError) as raised:
        await TaskConversationReader(fixture.store).load(fixture.observation)

    assert raised.value.code == "product-conversation-session-invalid"


async def test_unknown_usage_is_unavailable_not_zero_or_a_partial_total() -> None:
    fixture = await _build_fixture(coder_usage_quality="unknown")

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")

    assert coder.usage_state == "unavailable"
    assert coder.usage_tokens is None
    assert coder.usage_quality is None


async def test_shell_arguments_and_tool_result_content_are_never_exposed() -> None:
    fixture = await _build_fixture(coder_shell=True)

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")
    rendered = "\n".join(content for _kind, content in coder.messages)
    expected_size = len(
        canonical_json(
            {"command": "echo SECRET-IN-ARGUMENT\x1b[31m\u202e"}
        ).encode("utf-8")
    )

    assert coder.tool_calls == 1
    assert [kind for kind, _content in coder.messages] == ["input", "model", "tool"]
    assert f"shell <已遮蔽 · 参数 {expected_size} 字节>\t9–10\n成功" in rendered
    assert "call-coder" not in rendered
    assert "SECRET-IN-ARGUMENT" not in rendered
    assert "SECRET-IN-RESULT" not in rendered
    assert "\x1b" not in rendered
    assert "\u2028" not in rendered
    assert "\u202e" not in rendered


async def test_empty_non_shell_arguments_do_not_claim_sensitive_masking() -> None:
    fixture = await _build_fixture(coder_list_files=True)

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")
    rendered = "\n".join(content for _kind, content in coder.messages)

    assert "list_files\t9–10\n成功" in rendered
    assert "已遮蔽" not in rendered


async def test_search_text_keeps_its_safe_query_in_the_task_summary() -> None:
    fixture = await _build_fixture(coder_search_text=True)

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")
    rendered = "\n".join(content for _kind, content in coder.messages)

    assert "search_text reservation_handler\t9–10\n成功" in rendered
    assert "path" not in rendered


@pytest.mark.parametrize(
    ("exit_code", "expected", "forbidden"),
    (
        (0, "\n成功", "成功 · exit=0"),
        (1, "\n完成 · exit=1", "成功 · exit=1"),
    ),
)
async def test_shell_exit_code_controls_truthful_result_wording(
    exit_code: int,
    expected: str,
    forbidden: str,
) -> None:
    fixture = await _build_fixture(
        coder_shell=True,
        coder_shell_exit_code=exit_code,
    )

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")
    rendered = "\n".join(content for _kind, content in coder.messages)

    assert expected in rendered
    assert forbidden not in rendered


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("succeeded", "成功"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
        ("invalid", "无效"),
        ("denied", "已拒绝"),
        ("aborted_before_dispatch", "派发前中止"),
        ("unknown_after_crash", "结果未知"),
    ),
)
async def test_all_durable_tool_result_statuses_remain_projectable(
    status: str,
    expected: str,
) -> None:
    fixture = await _build_fixture(
        coder_shell=True,
        coder_shell_status=status,
    )

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    coder = next(role for role in snapshot.roles if role.role == "coder")

    assert coder.tool_calls == 1
    assert coder.messages[-1][0] == "tool"
    assert coder.messages[-1][1].endswith(f"\n{expected}")


async def test_unrelated_sessions_are_not_scanned_or_projected() -> None:
    fixture = await _build_fixture()
    await _append_session(
        fixture.store,
        "unrelated-session",
        role="unrelated",
    )

    snapshot = await TaskConversationReader(fixture.store).load(fixture.observation)
    rendered = "\n".join(
        content for role in snapshot.roles for _kind, content in role.messages
    )

    assert "unrelated" not in rendered
    assert {role.session_id for role in snapshot.roles} == set(fixture.sessions.values())
    assert fixture.store.list_stream_calls == 0


async def test_load_is_read_only_and_reopen_observes_new_durable_facts() -> None:
    fixture = await _build_fixture(coder_shell=True)
    reader = TaskConversationReader(fixture.store)
    before_heads = await _heads(fixture.store, fixture.sessions)
    before_appends = fixture.store.append_calls

    first = await reader.load(fixture.observation)

    assert await _heads(fixture.store, fixture.sessions) == before_heads
    assert fixture.store.append_calls == before_appends
    first_coder = next(role for role in first.roles if role.role == "coder")
    assert first_coder.turns_started == 1

    coder_session = fixture.sessions["coder"]
    stream = SessionService.session_stream(coder_session)
    head = await fixture.store.head(stream)
    await fixture.store.append(
        stream,
        expected_seq=head,
        events=_turn_events(
            coder_session,
            suffix="coder-followup",
            base_seq=head,
            input_text="fresh-followup-input",
            model_text="fresh-followup-output",
        ),
    )
    append_count_after_external_write = fixture.store.append_calls

    reopened = await reader.load(fixture.observation)

    assert fixture.store.append_calls == append_count_after_external_write
    reopened_coder = next(role for role in reopened.roles if role.role == "coder")
    assert reopened_coder.turns_started == 2
    assert reopened_coder.turns_completed == 2
    assert reopened_coder.usage_tokens == 24
    assert [kind for kind, _content in reopened_coder.messages] == [
        "input",
        "model",
        "tool",
        "input",
        "model",
    ]
    assert reopened_coder.messages[-1][1] == "fresh-followup-output"
    assert fixture.store.list_stream_calls == 0
