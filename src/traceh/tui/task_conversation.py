"""Fresh, read-only ProductTask role conversation projection for the TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from traceh.agents.directory import AgentDirectory, AgentDirectoryReader
from traceh.api.json_types import canonical_json
from traceh.api.llm import UsageQuality
from traceh.api.product import ProductRole, ProductTaskSummary
from traceh.cli.timeline import TimelineRenderer
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.observation import ProductObservation
from traceh.product.topology import product_role_node_id
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.tui.presentation import safe_display_block
from traceh.workflow.models import agent_identity

_ROLE_ORDER = ("router", "parent", "reviewer", "coder")
_ROLE_NODE_LABELS = {product_role_node_id(role): role.value for role in ProductRole}


@dataclass(frozen=True, slots=True)
class TaskConversationRole:
    role: str
    agent_id: str
    session_id: str
    turns_started: int
    turns_completed: int
    tool_calls: int
    usage_tokens: int | None
    usage_quality: str | None
    usage_state: str
    last_fact_age_seconds: int | None
    messages: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TaskConversationSnapshot:
    task_id: str
    roles: tuple[TaskConversationRole, ...]
    observed_at: datetime

    @property
    def default_role_index(self) -> int | None:
        if not self.roles:
            return None
        return min(
            range(len(self.roles)),
            key=lambda index: (
                self.roles[index].last_fact_age_seconds is None,
                self.roles[index].last_fact_age_seconds
                if self.roles[index].last_fact_age_seconds is not None
                else 0,
                index,
            ),
        )


class TaskConversationReader:
    __slots__ = ("_directory", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._directory = AgentDirectoryReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    async def load(
        self,
        observation: ProductObservation,
        *,
        observed_at: datetime | None = None,
    ) -> TaskConversationSnapshot:
        if type(observation) is not ProductObservation:
            raise ProductInputError("product-conversation-observation-invalid", "observation")
        summary = observation.summary
        if summary is None:
            raise ProductStateError(
                "product-conversation-summary-unavailable", observation.task_id
            )
        if summary.task_id != observation.task_id:
            raise ProductStateError(
                "product-conversation-task-identity-mismatch", observation.task_id
            )
        try:
            directory = await self._directory.load()
        except Exception:
            raise ProductStateError(
                "product-conversation-directory-unavailable", observation.task_id
            ) from None

        identities = self._role_identities(observation, summary, directory)
        now = datetime.now(UTC) if observed_at is None else observed_at
        if now.tzinfo is None:
            raise ProductInputError("product-conversation-time-invalid", "observed_at")
        roles: list[TaskConversationRole] = []
        for role, agent_id, session_id in identities:
            roles.append(
                await self._load_role(
                    task_id=observation.task_id,
                    role=role,
                    agent_id=agent_id,
                    session_id=session_id,
                    observed_at=now,
                )
            )
        roles.sort(key=lambda item: _ROLE_ORDER.index(item.role))
        return TaskConversationSnapshot(observation.task_id, tuple(roles), now)

    def _role_identities(
        self,
        observation: ProductObservation,
        summary: ProductTaskSummary,
        directory: AgentDirectory,
    ) -> tuple[tuple[str, str, str], ...]:
        identities: list[tuple[str, str, str]] = []
        known_streams = set(observation.related_streams)
        router_agent = summary.router_agent_id
        router_session = summary.routing_session_id
        if (router_agent is None) != (router_session is None):
            raise ProductStateError(
                "product-conversation-router-identity-incomplete", observation.task_id
            )
        if router_agent is not None and router_session is not None:
            record = directory.get(router_agent)
            if record is None:
                raise ProductStateError(
                    "product-conversation-agent-record-missing", observation.task_id
                )
            if record.session_id != router_session:
                raise ProductStateError(
                    "product-conversation-router-session-mismatch", observation.task_id
                )
            self._require_observed_stream(
                known_streams, router_session, observation.task_id
            )
            identities.append(("router", router_agent, router_session))

        evidence = observation.evidence
        if evidence is not None:
            for node in evidence.nodes:
                role = _ROLE_NODE_LABELS.get(node.node_id)
                if role is None:
                    continue
                if (node.agent_id is None) != (node.session_id is None):
                    raise ProductStateError(
                        "product-conversation-role-identity-incomplete",
                        observation.task_id,
                    )
                if node.agent_id is None or node.session_id is None:
                    continue
                expected_agent, expected_session, expected_request, _ = agent_identity(
                    summary.workflow_run_id, node.node_id
                )
                if (
                    node.agent_id != expected_agent
                    or node.session_id != expected_session
                ):
                    raise ProductStateError(
                        "product-conversation-role-identity-mismatch",
                        observation.task_id,
                    )
                record = directory.get(expected_agent)
                if record is None:
                    raise ProductStateError(
                        "product-conversation-agent-record-missing",
                        observation.task_id,
                    )
                if (
                    record.session_id != expected_session
                    or record.request_id != expected_request
                ):
                    raise ProductStateError(
                        "product-conversation-role-session-mismatch",
                        observation.task_id,
                    )
                self._require_observed_stream(
                    known_streams, expected_session, observation.task_id
                )
                identities.append((role, expected_agent, expected_session))

        session_ids = [session_id for _, _, session_id in identities]
        if len(session_ids) != len(set(session_ids)):
            raise ProductStateError(
                "product-conversation-session-duplicate", observation.task_id
            )
        return tuple(identities)

    @staticmethod
    def _require_observed_stream(
        known_streams: set[str], session_id: str, task_id: str
    ) -> None:
        if SessionService.session_stream(session_id) not in known_streams:
            raise ProductStateError(
                "product-conversation-session-unbound", task_id
            )

    async def _load_role(
        self,
        *,
        task_id: str,
        role: str,
        agent_id: str,
        session_id: str,
        observed_at: datetime,
    ) -> TaskConversationRole:
        try:
            events = await self._store.read(SessionService.session_stream(session_id))
            effects = await self._store.read(SessionService.effect_stream(session_id))
        except Exception:
            raise ProductStateError(
                "product-conversation-session-unreadable", task_id
            ) from None
        try:
            if CoreInvariantChecker().check(events, effects):
                raise ValueError("invalid Session lifecycle")
        except Exception:
            raise ProductStateError(
                "product-conversation-session-invalid", task_id
            ) from None

        timeline, entries = TimelineRenderer(), []
        pending_tools = {}
        for event in events:
            if event.type in {"user/message", "assistant/message"}:
                content = safe_display_block(
                    event.data.get("content", ""), limit=4_000, max_lines=40
                )
                if content:
                    kind = "model" if event.type == "assistant/message" else "input"
                    entries.append((event.seq, (kind, content)))
            elif event.type == "tool/call":
                call_id = event.data.get("tool_call_id")
                if type(call_id) is str:
                    rendered = (timeline.render(event) or "").removeprefix(
                        f"[event {event.seq}] Tool "
                    )
                    label, _, detail = rendered.partition(" requested")
                    if detail.startswith(" (call "):
                        detail = ""
                    arguments = event.data.get("arguments")
                    if not detail and isinstance(arguments, dict):
                        size = len(canonical_json(arguments).encode("utf-8"))
                        detail = f" <已遮蔽 · 参数 {size} 字节>"
                    pending_tools[call_id] = (event.seq, f"{label or '工具'}{detail}")
            elif event.type == "tool/result":
                call_id = event.data.get("tool_call_id")
                call = pending_tools.pop(call_id, None)
                if call is not None:
                    first_seq, label = call
                    status = {
                        "succeeded": "成功",
                        "failed": "失败",
                        "cancelled": "已取消",
                    }.get(event.data.get("status"), "状态不可用")
                    data = event.data.get("data")
                    exit_code = data.get("exit_code") if isinstance(data, dict) else None
                    if type(exit_code) is int:
                        status += f" · exit={exit_code}"
                    entries.append(
                        (first_seq, ("tool", f"{label}\t{first_seq}–{event.seq}\n{status}"))
                    )
        entries.extend(
            (seq, ("tool", f"{label}\t{seq}\n等待结果"))
            for seq, label in pending_tools.values()
        )
        entries.sort(key=lambda item: item[0])

        usage_tokens, usage_quality, usage_state = _usage(events)
        age = None
        if events:
            age = max(0, int((observed_at - events[-1].occurred_at).total_seconds()))
        return TaskConversationRole(
            role=role,
            agent_id=agent_id,
            session_id=session_id,
            turns_started=sum(event.type == "turn/start" for event in events),
            turns_completed=sum(event.type == "turn/end" for event in events),
            tool_calls=sum(event.type == "tool/call" for event in events),
            usage_tokens=usage_tokens,
            usage_quality=usage_quality,
            usage_state=usage_state,
            last_fact_age_seconds=age,
            messages=tuple(message for _, message in entries),
        )


def _usage(events) -> tuple[int | None, str | None, str]:
    starts = [event for event in events if event.type == "model/attempt-start"]
    ends = [event for event in events if event.type == "model/attempt-end"]
    if not starts and not ends:
        return None, None, "not_started"
    if len(starts) != len(ends):
        return None, None, "unavailable"
    total = 0
    qualities: list[UsageQuality] = []
    for event in ends:
        usage = event.data.get("usage")
        if not isinstance(usage, dict):
            return None, None, "unavailable"
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        recorded_total = usage.get("total_tokens")
        quality_value = usage.get("quality")
        if (
            type(input_tokens) is not int
            or input_tokens < 0
            or type(output_tokens) is not int
            or output_tokens < 0
            or type(recorded_total) is not int
            or recorded_total != input_tokens + output_tokens
        ):
            return None, None, "unavailable"
        try:
            quality = UsageQuality(quality_value)
        except (TypeError, ValueError):
            return None, None, "unavailable"
        if quality is UsageQuality.UNKNOWN:
            return None, None, "unavailable"
        total += recorded_total
        qualities.append(quality)
    quality = (
        UsageQuality.ESTIMATED
        if UsageQuality.ESTIMATED in qualities
        else UsageQuality.EXACT
    )
    return total, quality.value, "available"


__all__ = ["TaskConversationReader", "TaskConversationRole", "TaskConversationSnapshot"]
