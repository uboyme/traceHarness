"""Fresh, read-only ProductTask role conversation projection for the TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from traceh.api.json_types import canonical_json
from traceh.api.llm import UsageQuality
from traceh.cli.timeline import TimelineRenderer
from traceh.product.activity import ProductRoleActivity, ProductTaskActivityReader
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.observation import ProductObservation
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.tui.presentation import safe_display_block

_ROLE_ORDER = ("router", "parent", "reviewer", "coder")


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
    __slots__ = ("_activity", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._activity = ProductTaskActivityReader(store)

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
        try:
            activity = await self._activity.load(observation)
        except ProductStateError as error:
            code = error.code
            if code.startswith("product-activity-"):
                code = "product-conversation-" + code.removeprefix(
                    "product-activity-"
                )
            raise ProductStateError(code, observation.task_id) from None
        now = datetime.now(UTC) if observed_at is None else observed_at
        if now.tzinfo is None:
            raise ProductInputError("product-conversation-time-invalid", "observed_at")
        roles: list[TaskConversationRole] = []
        for role_activity in activity.roles:
            roles.append(
                await self._load_role(
                    task_id=observation.task_id,
                    activity=role_activity,
                    observed_at=now,
                )
            )
        roles.sort(key=lambda item: _ROLE_ORDER.index(item.role))
        return TaskConversationSnapshot(observation.task_id, tuple(roles), now)

    async def _load_role(
        self,
        *,
        task_id: str,
        activity: ProductRoleActivity,
        observed_at: datetime,
    ) -> TaskConversationRole:
        try:
            events = await self._store.read(
                SessionService.session_stream(activity.session_id)
            )
            effects = await self._store.read(
                SessionService.effect_stream(activity.session_id)
            )
        except Exception:
            raise ProductStateError(
                "product-conversation-session-unreadable", task_id
            ) from None
        try:
            if CoreInvariantChecker().check(events, effects):
                raise ValueError("invalid Session lifecycle")
            if (
                (events[-1].seq if events else 0) != activity.event_head_seq
                or (effects[-1].seq if effects else 0) != activity.effect_head_seq
            ):
                raise ValueError("Session changed during projection")
        except Exception:
            raise ProductStateError(
                "product-conversation-session-invalid", task_id
            ) from None

        timeline, entries = TimelineRenderer(), []
        tools = {tool.call_seq: tool for tool in activity.tools}
        for event in events:
            if event.type in {"user/message", "assistant/message"}:
                content = safe_display_block(
                    event.data.get("content", ""),
                    limit=None,
                    max_lines=None,
                    line_limit=None,
                )
                if content:
                    kind = "model" if event.type == "assistant/message" else "input"
                    entries.append((event.seq, (kind, content)))
            elif event.type == "tool/call":
                tool = tools.get(event.seq)
                if tool is not None:
                    rendered = (timeline.render(event) or "").removeprefix(
                        f"[event {event.seq}] Tool "
                    )
                    label, _, detail = rendered.partition(" requested")
                    if detail.startswith(" (call "):
                        detail = ""
                    arguments = event.data.get("arguments")
                    if (
                        event.data.get("tool_name") == "shell"
                        and isinstance(arguments, dict)
                    ):
                        size = len(canonical_json(arguments).encode("utf-8"))
                        detail = f" <已遮蔽 · 参数 {size} 字节>"
                    if tool.exit_code is not None:
                        status = (
                            "成功"
                            if tool.exit_code == 0
                            else f"完成 · exit={tool.exit_code}"
                        )
                    else:
                        status = {
                            "succeeded": "成功",
                            "failed": "失败",
                            "cancelled": "已取消",
                            "invalid": "无效",
                            "denied": "已拒绝",
                            "aborted_before_dispatch": "派发前中止",
                            "unknown_after_crash": "结果未知",
                            "pending": "等待结果",
                        }.get(tool.status, "状态不可用")
                    seq_range = (
                        str(tool.call_seq)
                        if tool.result_seq is None
                        else f"{tool.call_seq}–{tool.result_seq}"
                    )
                    entries.append(
                        (
                            tool.call_seq,
                            ("tool", f"{label or '工具'}{detail}\t{seq_range}\n{status}"),
                        )
                    )
        entries.sort(key=lambda item: item[0])

        usage_tokens, usage_quality, usage_state = _usage(events)
        age = None
        if events:
            age = max(0, int((observed_at - events[-1].occurred_at).total_seconds()))
        return TaskConversationRole(
            role=activity.role,
            agent_id=activity.agent_id,
            session_id=activity.session_id,
            turns_started=activity.turns_started,
            turns_completed=activity.turns_completed,
            tool_calls=activity.tool_call_count,
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
