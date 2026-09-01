"""Pure, bounded presentation values for the single Textual adapter.

Nothing in this module writes durable facts or decides a Product operation.
It translates the existing process-local proposal/start request, fresh Product
observation and host monotonic clock into an honest UI projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from unicodedata import combining, east_asian_width

from rich.cells import cell_len, chop_cells, set_cell_size
from rich.text import Text

from traceh.api.llm import UsageQuality
from traceh.api.product import ProductTaskStatus, RequestedTaskMode
from traceh.api.workflow import WorkflowStatus
from traceh.cli.command_line import escape_for_display
from traceh.product.chat import ProductStartRequest
from traceh.product.control import PendingProductProposal
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductUsage,
)

MAX_BLOCK_CHARS = 12_000
MAX_BLOCK_LINES = 160
MAX_LINE_CHARS = 800
STALL_WARNING_SECONDS = 20
MODEL_SELF_REPORT_COLOR = "#7d6bab"
_GROUP_SEPARATOR = "─" * 44


class ProductGateAction(StrEnum):
    START = "start"
    CANCEL = "cancel"
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class TransientProductState:
    """The process-local layer that deliberately does not survive restart."""

    kind: str
    operation: str = ""
    waiting_seconds: int = 0


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Pure rendering decision; the control plane remains authoritative."""

    actions: tuple[ProductGateAction, ...] = ()
    message: str = ""
    unknown: bool = False


@dataclass(frozen=True, slots=True)
class OperationErrorView:
    code: str
    guidance: str


@dataclass(frozen=True, slots=True)
class ProductIdentityField:
    """One exact identity shown by the explicit full-identity screen."""

    label: str
    value: str
    copy_key: str | None = None


def safe_display_block(
    value: object,
    *,
    limit: int | None = MAX_BLOCK_CHARS,
    max_lines: int | None = MAX_BLOCK_LINES,
    line_limit: int | None = MAX_LINE_CHARS,
) -> str:
    """Render an untrusted value as safe plain text with optional bounds."""

    text = value if type(value) is str else str(value)
    lines = text.split("\n")
    visible_lines = lines if max_lines is None else lines[:max_lines]
    truncated_lines = max_lines is not None and len(lines) > max_lines
    rendered = [
        escape_for_display(
            line,
            limit=(
                line_limit
                if line_limit is not None
                else max(1, len(line) * 8 + 1)
            ),
        )
        for line in visible_lines
    ]
    if truncated_lines:
        rendered.append("… (more lines omitted)")
    block = "\n".join(rendered)
    if limit is None or len(block) <= limit:
        return block
    return block[: limit - 1] + "…"


def prefixed_display_lines(
    content: str,
    *,
    width: int,
    first_prefix: str,
    continuation_prefix: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Wrap plain text to terminal cells before applying stable prefixes."""

    continuation = (
        " " * cell_len(first_prefix)
        if continuation_prefix is None
        else continuation_prefix
    )
    requested_prefix_width = max(
        cell_len(first_prefix),
        cell_len(continuation),
    )
    content_width = max(1, width - requested_prefix_width)
    prefix_width = max(0, width - content_width)
    first = set_cell_size(first_prefix, prefix_width)
    following = set_cell_size(continuation, prefix_width)

    rendered: list[tuple[str, str]] = []
    for physical_line in content.split("\n"):
        segments = chop_cells(physical_line, content_width) or [""]
        for segment in segments:
            prefix = first if not rendered else following
            rendered.append((prefix, segment))
    return tuple(rendered)


def resolve_gate(
    transient: TransientProductState,
    observation: ProductObservation | None,
) -> GateDecision:
    """Resolve the only Product actions the current three-layer view may show."""

    product = None if observation is None else observation.product_status
    workflow = None if observation is None else observation.workflow_status

    if transient.kind == "closing":
        return GateDecision(message="正在安全收敛，当前没有可执行闸门。")
    if transient.kind == "operation_pending":
        if (
            transient.operation == "start"
            and product is ProductTaskStatus.STARTED
            and workflow is WorkflowStatus.RUNNING
        ):
            return GateDecision(
                (ProductGateAction.CANCEL,),
                "任务正在执行；Cancel 会先收敛 START 调用，再走原控制面取消。",
            )
        return GateDecision(message="宿主操作尚未返回。")
    if transient.kind == "proposal" and product is None and workflow is None:
        return GateDecision(message="已提议；在对话中确认后才会出现 START。")
    if transient.kind == "start_request" and product is None and workflow is None:
        return GateDecision((ProductGateAction.START,), "需要独立 START 授权。")
    if transient.kind not in {"none", "proposal", "start_request"}:
        return GateDecision(
            message=f"未知进程内状态：{transient.kind}",
            unknown=True,
        )

    if transient.kind != "none":
        return _unknown_gate(transient, product, workflow)
    if product is None and workflow is None:
        return GateDecision()
    if product in {ProductTaskStatus.OPENED, ProductTaskStatus.ROUTED} and workflow is None:
        return GateDecision(message="任务已打开，宿主正在推进下一条 durable 事实。")
    if product is ProductTaskStatus.STARTED and workflow is WorkflowStatus.RUNNING:
        return GateDecision((ProductGateAction.CANCEL,), "任务正在执行。")
    if (
        product is ProductTaskStatus.STARTED
        and workflow is WorkflowStatus.AWAITING_APPROVAL
    ):
        return GateDecision(
            message=(
                "Product 与 Workflow 尚未对账；本轮 TUI 不提供写入型对账快捷键，"
                "可在 Line 界面执行精确 /task inspect。"
            )
        )
    if (
        product is ProductTaskStatus.AWAITING_APPROVAL
        and workflow is WorkflowStatus.AWAITING_APPROVAL
    ):
        if _approval_ready(observation):
            return GateDecision(
                (ProductGateAction.APPROVE, ProductGateAction.REJECT),
                "Review 与审批证据已由 fresh facts 建立。",
            )
        return GateDecision(message="审批证据尚不完整；批准入口保持关闭。")
    if product in {
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    }:
        return GateDecision()
    return _unknown_gate(transient, product, workflow)


def task_handle(requirement: str | None, task_id: str | None) -> str:
    """Produce a display-only short handle without creating an identity alias."""

    if requirement:
        first_line = safe_display_block(requirement, limit=120, max_lines=1).strip()
        if first_line:
            return _truncate_columns(first_line, 12)
    if task_id:
        return f"ProductTask · {safe_display_block(task_id[:4], limit=12)}"
    return "ProductTask"


def product_panel_text(
    *,
    product_enabled: bool,
    proposal: PendingProductProposal | None,
    start_request: ProductStartRequest | None,
    observation: ProductObservation | None,
    transient: TransientProductState,
    now_monotonic: float,
    observation_received_at: float | None,
    operation_error: OperationErrorView | None,
    observation_error: OperationErrorView | None,
) -> str:
    """Render the default Product view from current facts and transient state."""

    if not product_enabled:
        return "ProductTask 未启用\n\n本次 Chat 没有装配 Product 配置。"
    pending = start_request.pending if start_request is not None else proposal
    if pending is None and observation is None:
        if observation_error is not None:
            return safe_display_block(
                "ProductTask 状态暂不可读\n\n"
                f"Durable observation 暂不可用 · {observation_error.code}\n"
                f"{observation_error.guidance}\n"
                "宿主会按当前刷新周期重新读取。"
            )
        if operation_error is not None:
            return safe_display_block(
                "尚无 ProductTask\n\n"
                f"宿主操作未完成 · {operation_error.code}\n"
                f"{operation_error.guidance}"
            )
        return (
            "尚无 ProductTask\n\n"
            "描述需求后，模型可以提出 ProductTask；你确认后，宿主才会显示 START。\n\n"
            "当前：尚无提案"
        )

    task_id = (
        observation.task_id
        if observation is not None
        else None if pending is None else pending.task_id
    )
    requirement = None if pending is None else pending.requirement
    header = [
        task_handle(requirement, task_id),
        _subtitle(pending, observation),
        "",
        _lifecycle(pending, start_request, observation, transient),
    ]
    facts = ["最近 durable 事实"]
    evidence = list(_review_lines(observation))
    terminal: list[str] = []

    if transient.kind == "operation_pending":
        operation = transient.operation.upper() or "操作"
        waited = format_age(transient.waiting_seconds)
        terminal.append(f"{operation} 已被宿主接受 · 等待返回 {waited}")
        if transient.waiting_seconds >= STALL_WARNING_SECONDS:
            latest = _latest_fact_age(
                observation,
                now_monotonic=now_monotonic,
                observation_received_at=observation_received_at,
            )
            if latest is not None:
                terminal.append(f"警告：无新任务事实 {format_age(latest)}")
            else:
                terminal.append("警告：尚未观察到 durable 任务事实")

    if operation_error is not None:
        terminal.extend(
            (
                f"宿主操作未完成 · {safe_display_block(operation_error.code, limit=120)}",
                safe_display_block(operation_error.guidance, limit=300),
            )
        )

    if observation_error is not None:
        terminal.extend(
            (
                "Durable observation 暂不可用 · "
                f"{safe_display_block(observation_error.code, limit=120)}",
                safe_display_block(observation_error.guidance, limit=300),
                "宿主会按当前刷新周期重新读取；现有 durable 事实未被改写。",
            )
        )

    if observation is not None:
        fact_lines = _fact_lines(
            observation,
            now_monotonic=now_monotonic,
            observation_received_at=observation_received_at,
        )
        facts.extend(fact_lines or ("  尚无 durable 事实",))
        symptom = _derived_symptom(observation)
        if symptom:
            facts.append(symptom)
        if observation.streams_diverged:
            product = _status_value(observation.product_status)
            workflow = _status_value(observation.workflow_status)
            facts.extend(
                (
                    "",
                    f"未对账：Product {product} ┊ Workflow {workflow}",
                    "对账是显式写操作，不会由刷新偷偷执行。",
                )
            )
        summary = observation.summary
        if summary is not None and summary.settled:
            leaf = _leaf_failure_line(observation)
            if leaf:
                terminal.append(leaf)
            terminal.append(_terminal_line(summary.status, summary.failure_code))
    else:
        facts.append("  尚无 durable 事实")

    return _panel_groups(header, facts, evidence, terminal)


def product_state_text(content: object, *, terminal: bool) -> Text:
    """Return safe Product text with only a terminal lifecycle track muted."""

    rendered = safe_display_block(content)
    text = Text(rendered)
    if not terminal:
        return text
    span = _lifecycle_span(rendered)
    if span is not None:
        text.stylize("dim", *span)
    return text


def _panel_groups(*groups: list[str]) -> str:
    lines: list[str] = []
    for group in groups:
        if lines:
            lines.extend(("", _GROUP_SEPARATOR, ""))
        lines.extend(group)
    return safe_display_block("\n".join(lines))


def product_identity_fields(
    chat_session_id: str | None,
    proposal: PendingProductProposal | None,
    start_request: ProductStartRequest | None,
    observation: ProductObservation | None,
) -> tuple[ProductIdentityField, ...]:
    """Return the exact current identities; no value here is abbreviated."""

    pending = start_request.pending if start_request is not None else proposal
    summary = None if observation is None else observation.summary
    review = None if observation is None else observation.review
    task_id = observation.task_id if observation is not None else None
    if task_id is None and pending is not None:
        task_id = pending.task_id
    rows: list[ProductIdentityField] = []

    def add(label: str, value: object | None, copy_key: str | None = None) -> None:
        if value is not None:
            rows.append(ProductIdentityField(label, str(value), copy_key))

    add("task", task_id, "c")
    add("session", chat_session_id, "s")
    add("profile", None if pending is None else pending.profile_id)
    add("profile_digest", None if summary is None else summary.profile_digest)
    add("workflow_run", None if summary is None else summary.workflow_run_id)
    add("source_revision", None if summary is None else summary.source_base_revision)
    add("origin_session", None if summary is None else summary.origin_session_id)
    add("origin_turn", None if summary is None else summary.origin_turn_id)
    add("origin_message", None if summary is None else summary.origin_message_id)
    add(
        "confirmation_session",
        None if summary is None else summary.confirmation_session_id,
    )
    add("router_agent", None if summary is None else summary.router_agent_id)
    add("router_session", None if summary is None else summary.routing_session_id)
    if observation is not None and observation.evidence is not None:
        for node in observation.evidence.nodes:
            if node.agent_id is not None:
                add(f"{node.node_id}.agent", node.agent_id)
            if node.session_id is not None:
                add(f"{node.node_id}.session", node.session_id)
    add("review", None if review is None else review.review_id, "r")
    add(
        "target",
        None if review is None else f"{review.target_ref} @ {review.expected_revision}",
        "t",
    )
    add("patch", None if review is None else review.patch_sha256, "p")
    add(
        "digest",
        None if observation is None else observation.approval_digest,
        "d",
    )
    add(
        "approval_operation",
        None if observation is None or observation.approval is None
        else observation.approval.operation_id,
    )
    add(
        "promotion",
        None if observation is None or observation.promotion is None
        else observation.promotion.promotion_id,
    )
    return tuple(rows)


def product_compact_text(
    *,
    product_enabled: bool,
    proposal: PendingProductProposal | None,
    start_request: ProductStartRequest | None,
    observation: ProductObservation | None,
    transient: TransientProductState,
    now_monotonic: float,
    observation_received_at: float | None,
    operation_error: OperationErrorView | None,
    observation_error: OperationErrorView | None,
) -> str:
    """Render the narrow two-line body; the gate area is the third line."""

    if not product_enabled:
        return "ProductTask 未启用\n本次 Chat 没有 Product 配置"
    pending = start_request.pending if start_request is not None else proposal
    if pending is None and observation is None:
        if observation_error is not None:
            return safe_display_block(
                "ProductTask 状态暂不可读\n"
                f"Observation · {observation_error.code}",
                limit=600,
                max_lines=2,
            )
        if operation_error is not None:
            return safe_display_block(
                "尚无 ProductTask\n"
                f"宿主操作未完成 · {operation_error.code}",
                limit=600,
                max_lines=2,
            )
        return "尚无 ProductTask\n尚无 durable 事实"
    task_id = (
        observation.task_id
        if observation is not None
        else None if pending is None else pending.task_id
    )
    requirement = None if pending is None else pending.requirement
    lifecycle = _lifecycle(pending, start_request, observation, transient)
    first = f"{task_handle(requirement, task_id)} · {lifecycle}"
    summary = None if observation is None else observation.summary
    if observation_error is not None:
        second = (
            "Observation 暂不可用 · "
            f"{safe_display_block(observation_error.code, limit=120, max_lines=1)}"
        )
    elif operation_error is not None:
        second = (
            "宿主操作未完成 · "
            f"{safe_display_block(operation_error.code, limit=120, max_lines=1)}"
        )
    elif transient.kind == "operation_pending":
        second = (
            f"{transient.operation.upper() or '操作'} 已等待 "
            f"{format_age(transient.waiting_seconds)}"
        )
        if transient.waiting_seconds >= STALL_WARNING_SECONDS:
            second += " · 无新任务事实"
    elif summary is not None and summary.status is ProductTaskStatus.FAILED:
        failure = _leaf_failure_line(observation) or (
            summary.failure_code or "product-task-failed"
        )
        second = (
            "任务失败 · "
            f"{safe_display_block(failure, limit=120, max_lines=1)}"
        )
    else:
        age = _latest_fact_age(
            observation,
            now_monotonic=now_monotonic,
            observation_received_at=observation_received_at,
        )
        second = (
            "尚无任务事实"
            if age is None
            else f"最近任务事实 · {format_age(age)}前"
        )
    return safe_display_block(f"{first}\n{second}", limit=600, max_lines=2)


def operation_error_view(code: str) -> OperationErrorView:
    guidance = {
        "workspace-source-invalid": (
            "这是启动后的 Product operation 错误。请检查 source workspace "
            "是否存在未提交或未忽略文件，修复后再重试。"
        ),
        "product-observation-unavailable": (
            "只读任务观察暂时不可用。durable 事实没有被界面改写；"
            "可稍后刷新或使用 inspect/replay 核对。"
        ),
        "product-router-agent-failed": (
            "Router Agent 没有形成可用结果。请查看最近 Session 事实；系统不会自动 fallback。"
        ),
    }.get(
        code,
        "这是宿主操作错误，不是 durable 任务终态。请根据稳定错误码修复条件后重试。",
    )
    return OperationErrorView(code=code, guidance=guidance)


def format_age(seconds: float | int) -> str:
    value = max(0, int(seconds))
    if value < 60:
        return f"{value} 秒"
    minutes, remaining = divmod(value, 60)
    if minutes < 60:
        return f"{minutes} 分 {remaining} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _approval_ready(observation: ProductObservation | None) -> bool:
    return bool(
        observation is not None
        and not observation.streams_diverged
        and observation.review is not None
        and observation.evidence is not None
        and observation.approval_digest is not None
        and observation.approval is None
        and observation.promotion is None
    )


def _unknown_gate(
    transient: TransientProductState,
    product: ProductTaskStatus | None,
    workflow: WorkflowStatus | None,
) -> GateDecision:
    return GateDecision(
        message=(
            f"transient={transient.kind}; product={_status_value(product)}; "
            f"workflow={_status_value(workflow)}"
        ),
        unknown=True,
    )


def _subtitle(
    pending: PendingProductProposal | None,
    observation: ProductObservation | None,
) -> str:
    summary = None if observation is None else observation.summary
    requested = (
        pending.proposal.requested_mode.value
        if pending is not None
        else "unknown" if summary is None else summary.requested_mode.value
    )
    resolved = (
        None
        if summary is None or summary.resolved_mode is None
        else summary.resolved_mode.value
    )
    if requested == RequestedTaskMode.AUTO.value:
        mode = f"auto → {resolved or '待路由'}"
    else:
        mode = requested
    profile = pending.profile_id if pending is not None else "profile 已冻结"
    target = "target 待建立"
    if pending is not None:
        target = _short_ref(pending.proposal.preflight.promotion_target_ref)
    elif observation is not None and observation.review is not None:
        target = _short_ref(observation.review.target_ref)
    return safe_display_block(
        f"{mode} · {profile} · → {target}", limit=300, max_lines=1
    )


def _lifecycle(
    pending: PendingProductProposal | None,
    start_request: ProductStartRequest | None,
    observation: ProductObservation | None,
    transient: TransientProductState,
) -> str:
    summary = None if observation is None else observation.summary
    proposed = pending is not None or summary is not None
    confirmed = start_request is not None or summary is not None
    transient_track = (
        f"进程内 提议 {'✓' if proposed else '·'} · 确认 {'✓' if confirmed else '·'}"
    )
    requested = (
        pending.proposal.requested_mode
        if pending is not None
        else None if summary is None else summary.requested_mode
    )
    stages: list[str] = []
    status = None if summary is None else summary.status
    stages.append(
        _stage(
            "开",
            status is not None,
            status is None and transient.kind == "operation_pending",
        )
    )
    if requested is RequestedTaskMode.AUTO:
        routed = status is not None and status is not ProductTaskStatus.OPENED
        routing = status is ProductTaskStatus.OPENED and not summary.settled
        stages.append(_stage("路由", routed, routing))
    execution_done = status in {
        ProductTaskStatus.AWAITING_APPROVAL,
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    }
    executing = status in {ProductTaskStatus.ROUTED, ProductTaskStatus.STARTED}
    stages.append(_stage("执行", execution_done, executing))
    approval_done = status in {
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
    }
    approving = status is ProductTaskStatus.AWAITING_APPROVAL
    stages.append(_stage("审批", approval_done, approving))
    stages.append(_stage("合入", status is ProductTaskStatus.COMPLETED, False))
    return transient_track + " ┊ durable " + " · ".join(stages)


def _stage(label: str, done: bool, current: bool) -> str:
    if done:
        return f"{label} ✓"
    if current:
        return f"{label} ⋯"
    return label


def _fact_lines(
    observation: ProductObservation,
    *,
    now_monotonic: float,
    observation_received_at: float | None,
) -> list[str]:
    roles: dict[str, str] = {}
    summary = observation.summary
    if summary is not None:
        roles[f"session:{summary.origin_session_id}"] = "chat"
        roles[f"session:{summary.confirmation_session_id}"] = "chat"
        if summary.routing_session_id is not None:
            roles[f"session:{summary.routing_session_id}"] = "router"
    if observation.evidence is not None:
        for node in observation.evidence.nodes:
            if node.session_id is not None:
                roles[f"session:{node.session_id}"] = node.node_id
    lines: list[str] = []
    for head in observation.stream_heads:
        label = _stream_label(head, observation.task_id, roles)
        if label is None or head.seq == 0 or head.event_type is None:
            continue
        age = _head_age(
            head,
            observation,
            now_monotonic=now_monotonic,
            observation_received_at=observation_received_at,
        )
        event_type = safe_display_block(head.event_type, limit=100, max_lines=1)
        age_text = f"{format_age(age)}前"
        lines.append(
            f"{_fixed_column(label, 17)} "
            f"{_fixed_column(event_type, 21)} "
            f"{_fixed_column(age_text, 12, right=True)}"
        )
    return lines


def _stream_label(
    head: ObservedStreamHead,
    task_id: str,
    roles: dict[str, str],
) -> str | None:
    if head.stream_id == f"product-task:{task_id}":
        return "product"
    if head.stream_id == f"workflow:{task_id}":
        return "workflow"
    role = roles.get(head.stream_id)
    if role is None:
        return None
    product_role_prefix = "product-role-"
    short_role = (
        role[len(product_role_prefix) :]
        if role.startswith(product_role_prefix) and len(role) > len(product_role_prefix)
        else role
    )
    return f"session·{short_role}"


def _head_age(
    head: ObservedStreamHead,
    observation: ProductObservation,
    *,
    now_monotonic: float,
    observation_received_at: float | None,
) -> float:
    if head.occurred_at is None:
        return 0.0
    initial = max(0.0, (observation.observed_at - head.occurred_at).total_seconds())
    if observation_received_at is None:
        return initial
    return initial + max(0.0, now_monotonic - observation_received_at)


def _latest_fact_age(
    observation: ProductObservation | None,
    *,
    now_monotonic: float,
    observation_received_at: float | None,
) -> float | None:
    if observation is None:
        return None
    ages = [
        _head_age(
            head,
            observation,
            now_monotonic=now_monotonic,
            observation_received_at=observation_received_at,
        )
        for head in observation.stream_heads
        if head.task_bound and head.seq > 0 and head.occurred_at is not None
    ]
    return min(ages) if ages else None


def _derived_symptom(observation: ProductObservation) -> str:
    summary = observation.summary
    if (
        summary is None
        or summary.requested_mode is not RequestedTaskMode.AUTO
        or summary.status is not ProductTaskStatus.OPENED
        or summary.routing_session_id is None
    ):
        return ""
    product = _find_head(observation, f"product-task:{observation.task_id}")
    router = _find_head(observation, f"session:{summary.routing_session_id}")
    if (
        product is not None
        and product.event_type == "product/task-opened"
        and router is not None
        and router.event_type == "turn/end"
    ):
        return (
            "Router Session 已结束；ProductTask 尚未记录 routing。"
            "这是症状描述，不是根因判断。"
        )
    return ""


def _find_head(
    observation: ProductObservation, stream_id: str
) -> ObservedStreamHead | None:
    return next(
        (head for head in observation.stream_heads if head.stream_id == stream_id),
        None,
    )


def _review_lines(observation: ProductObservation | None) -> tuple[str, ...]:
    usage = None if observation is None else observation.usage
    lines = ["证据", _usage_line(usage)]
    review = None if observation is None else observation.review
    if review is None:
        lines.append("  尚未建立")
        return tuple(lines)
    digest = observation.approval_digest
    review_id = safe_display_block(review.review_id[:12], limit=24)
    revision = safe_display_block(review.expected_revision[:12], limit=24)
    patch = safe_display_block(review.patch_sha256[:12], limit=24)
    digest_text = (
        "unavailable"
        if digest is None
        else safe_display_block(digest[:12], limit=24) + "…"
    )
    lines.extend(
        (
        f"  审批    {review_id}…  →  {_short_ref(review.target_ref)} @ {revision}…",
        f"          patch {patch}… · digest {digest_text}",
        )
    )
    evidence = None if observation.evidence is None else observation.evidence.review
    if evidence is None:
        return tuple(lines)

    paths = " · ".join(_safe_full_label(path) for path in evidence.changed_paths)
    lines.append(f"  改动    {paths or '无'}")

    for index, verifier in enumerate(evidence.verifiers):
        exit_code = "unavailable" if verifier.exit_code is None else str(verifier.exit_code)
        label = "校验" if index == 0 else "    "
        lines.append(
            f"  {label}    "
            f"{safe_display_block(verifier.command_id, limit=80, max_lines=1)} · "
            f"{safe_display_block(verifier.status, limit=40, max_lines=1)} · "
            f"exit={exit_code} · argv={safe_display_block(verifier.argv_digest[:12], limit=24)}…"
        )
    if not evidence.verifiers:
        lines.append("  校验    未记录")

    summary = evidence.patch_summary
    file_count = len(evidence.changed_paths)
    additions = deletions = None
    if summary is not None:
        file_count = len(summary.files)
        additions = summary.additions
        deletions = summary.deletions
    lines.append(
        f"  补丁    {evidence.patch_size_bytes} bytes · {file_count} 文件 · "
        f"{_change_count('+', additions)} {_change_count('−', deletions)}"
    )
    if summary is None:
        for path in evidence.changed_paths:
            lines.append(
                "          "
                f"{_safe_full_label(path)}"
                " · 状态未知"
            )
    else:
        for file in summary.files:
            path = _safe_full_label(file.path)
            status = _patch_status_text(file.status)
            counts = (
                "二进制"
                if file.binary
                else f"{_change_count('+', file.additions)} {_change_count('−', file.deletions)}"
            )
            lines.append(f"          {path} · {status} · {counts}")
    lines.append("          ^d 查看完整改动 · ^p 查看完整身份")
    return tuple(lines)


def _change_count(marker: str, value: int | None) -> str:
    return f"{marker}{'?' if value is None else value}"


def _safe_full_label(value: str) -> str:
    limit = max(1, len(value) * 8 + 1)
    return safe_display_block(
        value,
        limit=limit,
        max_lines=1,
        line_limit=limit,
    )


def _patch_status_text(status: str) -> str:
    return {
        "added": "新增",
        "modified": "修改",
        "deleted": "删除",
        "renamed": "重命名",
    }.get(status, "状态未知")


def _terminal_line(status: ProductTaskStatus, failure_code: str | None) -> str:
    if status is ProductTaskStatus.COMPLETED:
        return "已合入 · Promotion receipt 已记录"
    if status is ProductTaskStatus.FAILED:
        code = failure_code or "product-task-failed"
        return (
            f"任务已记录失败 · {safe_display_block(code, limit=120)}；"
            "查看证据后创建新任务。"
        )
    return f"任务终态：{status.value}。"


def _usage_line(usage: ProductUsage | None) -> str:
    tokens = "—"
    steps = "—"
    elapsed = "—"
    if usage is not None:
        if usage.tokens is not None:
            prefix = "约 " if usage.token_quality is UsageQuality.ESTIMATED else ""
            tokens = f"{prefix}{usage.tokens}"
        if usage.steps is not None:
            steps = str(usage.steps)
        if usage.wall_milliseconds is not None:
            elapsed = _format_duration(usage.wall_milliseconds)
    return f"  用量   {tokens} tok · {steps} 步 · 用时 {elapsed}"


def _format_duration(milliseconds: int) -> str:
    if milliseconds == 0:
        return "0 秒"
    if milliseconds < 1_000:
        return "<1 秒"
    return format_age(milliseconds // 1_000)


def _lifecycle_span(rendered: str) -> tuple[int, int] | None:
    lines = rendered.splitlines(keepends=True)
    if len(lines) >= 4:
        lifecycle = lines[3].rstrip("\r\n")
        if lifecycle.startswith("进程内 ") and "┊ durable " in lifecycle:
            start = sum(len(line) for line in lines[:3])
            return start, start + len(lifecycle)
    if lines:
        first = lines[0].rstrip("\r\n")
        marker = " · 进程内 "
        marker_at = first.rfind(marker)
        if marker_at >= 0 and "┊ durable " in first[marker_at:]:
            start = marker_at + len(" · ")
            return start, len(first)
    return None


def _leaf_failure_line(observation: ProductObservation | None) -> str:
    evidence = None if observation is None else observation.evidence
    if evidence is None:
        return ""
    unavailable_node: str | None = None
    for node in evidence.nodes:
        if node.leaf_failure_code is not None:
            category = (
                ""
                if node.leaf_failure_category is None
                else f" · {node.leaf_failure_category}"
            )
            return safe_display_block(
                f"叶子失败：{node.node_id} · {node.leaf_failure_code}{category}",
                limit=300,
                max_lines=1,
            )
        if node.leaf_error_type is not None:
            return safe_display_block(
                f"叶子失败：{node.node_id} · {node.leaf_error_type}",
                limit=300,
                max_lines=1,
            )
        if (
            unavailable_node is None
            and node.failure_code == "workflow-agent-message-failed"
        ):
            unavailable_node = node.node_id
    if unavailable_node is not None:
        return safe_display_block(
            f"叶子失败：{unavailable_node} · unavailable",
            limit=300,
            max_lines=1,
        )
    return ""


def _short_ref(value: str) -> str:
    prefix = "refs/heads/"
    short = value[len(prefix) :] if value.startswith(prefix) else value
    return safe_display_block(short, limit=120, max_lines=1)


def _status_value(value: object | None) -> str:
    raw = getattr(value, "value", value)
    return "none" if raw is None else safe_display_block(raw, limit=80, max_lines=1)


def _truncate_columns(value: str, max_columns: int) -> str:
    truncated = _clip_columns(value, max_columns)
    if _display_columns(value) > max_columns and truncated:
        truncated = _clip_columns(value, max_columns - 1) + "…"
    return truncated or "ProductTask"


def _fixed_column(value: str, width: int, *, right: bool = False) -> str:
    clipped = _clip_columns(value, width)
    if _display_columns(value) > width:
        clipped = _clip_columns(value, width - 1) + "…"
    padding = " " * max(0, width - _display_columns(clipped))
    return f"{padding}{clipped}" if right else f"{clipped}{padding}"


def _clip_columns(value: str, max_columns: int) -> str:
    width = 0
    output: list[str] = []
    for character in value:
        character_width = _character_columns(character)
        if width + character_width > max_columns:
            break
        output.append(character)
        width += character_width
    return "".join(output)


def _display_columns(value: str) -> int:
    return sum(_character_columns(character) for character in value)


def _character_columns(character: str) -> int:
    if combining(character):
        return 0
    return 2 if east_asian_width(character) in {"W", "F"} else 1


__all__ = [
    "MAX_BLOCK_CHARS",
    "MAX_BLOCK_LINES",
    "MAX_LINE_CHARS",
    "MODEL_SELF_REPORT_COLOR",
    "STALL_WARNING_SECONDS",
    "GateDecision",
    "OperationErrorView",
    "ProductGateAction",
    "ProductIdentityField",
    "TransientProductState",
    "format_age",
    "operation_error_view",
    "product_compact_text",
    "product_identity_fields",
    "product_panel_text",
    "resolve_gate",
    "safe_display_block",
    "prefixed_display_lines",
    "task_handle",
]
