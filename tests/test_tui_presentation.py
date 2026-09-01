"""Core-only contracts for the single current TUI presentation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unicodedata import combining, east_asian_width

from product_fixtures import ORIGIN_SESSION, preflight, proposal
from rich.cells import cell_len

from traceh.api.llm import UsageQuality
from traceh.api.product import (
    ProductTaskStatus,
    ProductTaskSummary,
)
from traceh.api.workflow import WorkflowStatus
from traceh.artifacts.unified_diff import (
    UnifiedDiffFileSummary,
    UnifiedDiffSummary,
)
from traceh.product.chat import ProductStartRequest
from traceh.product.control import PendingProductProposal
from traceh.product.inspection import (
    ProductNodeEvidence,
    ProductReviewEvidence,
    ProductTaskEvidence,
    ProductVerifierEvidence,
)
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductUsage,
)
from traceh.tui.presentation import (
    OperationErrorView,
    ProductGateAction,
    TransientProductState,
    prefixed_display_lines,
    product_compact_text,
    product_identity_fields,
    product_panel_text,
    product_state_text,
    resolve_gate,
    task_handle,
)

NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)


def test_prefixed_display_lines_wraps_cells_before_reapplying_indent() -> None:
    lines = prefixed_display_lines(
        "库存预留abcdefghijk\n第二行",
        width=18,
        first_prefix="  ▏ 模型 · ",
        continuation_prefix="  ▏        ",
    )

    assert len(lines) >= 3
    assert lines[0][0] == "  ▏ 模型 · "
    assert all(prefix == "  ▏        " for prefix, _content in lines[1:])
    assert all(cell_len(prefix + content) <= 18 for prefix, content in lines)
    assert "".join(content for _prefix, content in lines) == (
        "库存预留abcdefghijk第二行"
    )


def _summary(status: ProductTaskStatus) -> ProductTaskSummary:
    offered = proposal(binding=preflight(), session_id=ORIGIN_SESSION)
    return ProductTaskSummary(
        task_id="task-presentation",
        status=status,
        requested_mode=offered.requested_mode,
        mode_source=offered.mode_source,
        requirement_digest=offered.requirement_digest,
        profile_digest=offered.preflight.profile_digest,
        preflight_digest=offered.preflight.digest,
        origin_session_id=ORIGIN_SESSION,
        origin_turn_id="origin-turn",
        origin_message_id="origin-message",
        confirmation_session_id=ORIGIN_SESSION,
        confirmation_turn_id="confirm-turn",
        confirmation_message_id="confirm-message",
        head_seq=1,
    )


def _observation(
    status: ProductTaskStatus,
    workflow: WorkflowStatus | None,
    *,
    approval_ready: bool = False,
) -> ProductObservation:
    review = None
    evidence = None
    digest = None
    if approval_ready:
        review = SimpleNamespace(
            review_id="review-presentation",
            patch_sha256="a" * 64,
            target_ref="refs/heads/main",
            expected_revision="b" * 40,
        )
        evidence = ProductTaskEvidence("awaiting_approval", (), None)
        digest = "d" * 64
    return ProductObservation(
        task_id="task-presentation",
        summary=_summary(status),
        workflow=(
            None if workflow is None else SimpleNamespace(status=workflow)
        ),
        evidence=evidence,
        review=review,
        approval=None,
        promotion=None,
        approval_digest=digest,
        stream_heads=(
            ObservedStreamHead(
                "product-task:task-presentation",
                1,
                f"product/task-{status.value}",
                NOW - timedelta(seconds=3),
                True,
            ),
        ),
        observed_at=NOW,
    )


def _start_request() -> ProductStartRequest:
    pending = PendingProductProposal(
        task_id="task-presentation",
        proposal=proposal(binding=preflight(), session_id=ORIGIN_SESSION),
        profile_id="profile-presentation",
        requirement="Improve a generic reservation operation safely",
    )
    return ProductStartRequest(
        pending=pending,
        session_id=ORIGIN_SESSION,
        confirming_turn_id="confirm-turn",
        confirming_message_id="confirm-message",
    )


def test_resolve_gate_covers_every_frozen_legal_combination() -> None:
    assert resolve_gate(TransientProductState("none"), None).actions == ()
    assert resolve_gate(TransientProductState("proposal"), None).actions == ()
    assert resolve_gate(
        TransientProductState("start_request"), None
    ).actions == (ProductGateAction.START,)
    assert resolve_gate(
        TransientProductState("operation_pending", "start", 4), None
    ).actions == ()
    assert resolve_gate(
        TransientProductState("operation_pending", "start", 4),
        _observation(ProductTaskStatus.STARTED, WorkflowStatus.RUNNING),
    ).actions == (ProductGateAction.CANCEL,)
    assert resolve_gate(
        TransientProductState("none"),
        _observation(ProductTaskStatus.OPENED, None),
    ).actions == ()
    assert resolve_gate(
        TransientProductState("none"),
        _observation(ProductTaskStatus.STARTED, WorkflowStatus.RUNNING),
    ).actions == (ProductGateAction.CANCEL,)
    assert resolve_gate(
        TransientProductState("none"),
        _observation(
            ProductTaskStatus.STARTED,
            WorkflowStatus.AWAITING_APPROVAL,
        ),
    ).actions == ()
    assert resolve_gate(
        TransientProductState("none"),
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
            approval_ready=True,
        ),
    ).actions == (ProductGateAction.APPROVE, ProductGateAction.REJECT)
    for status in (
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    ):
        assert resolve_gate(
            TransientProductState("none"), _observation(status, None)
        ).actions == ()
    assert resolve_gate(TransientProductState("closing"), None).actions == ()


def test_resolve_gate_fails_closed_for_incomplete_or_unknown_evidence() -> None:
    incomplete = resolve_gate(
        TransientProductState("none"),
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
        ),
    )
    assert incomplete.actions == ()
    assert "证据尚不完整" in incomplete.message

    unknown = resolve_gate(
        TransientProductState("none"),
        _observation(ProductTaskStatus.STARTED, None),
    )
    assert unknown.actions == ()
    assert unknown.unknown
    assert "product=started" in unknown.message


def test_start_wait_is_immediate_and_age_advances_without_fake_progress() -> None:
    request = _start_request()
    observation = replace(
        _observation(ProductTaskStatus.OPENED, None),
        stream_heads=(
            ObservedStreamHead(
                "product-task:task-presentation",
                1,
                "product/task-opened",
                NOW - timedelta(seconds=19),
                True,
            ),
        ),
    )
    rendered = product_panel_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=observation,
        transient=TransientProductState("operation_pending", "start", 25),
        now_monotonic=106.0,
        observation_received_at=100.0,
        operation_error=None,
        observation_error=None,
    )
    assert "START 已被宿主接受 · 等待返回 25 秒" in rendered
    assert "警告：无新任务事实 25 秒" in rendered
    assert "preflight" not in rendered
    assert "模型正在思考" not in rendered


def test_gate_instruction_is_rendered_once_outside_the_summary() -> None:
    request = _start_request()
    transient = TransientProductState("start_request")
    rendered = product_panel_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=None,
        transient=transient,
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    gate = resolve_gate(transient, None)
    assert gate.message
    assert gate.message not in rendered

    completed = _observation(
        ProductTaskStatus.COMPLETED,
        WorkflowStatus.COMPLETED,
    )
    completed_panel = product_panel_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=completed,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    completed_gate = resolve_gate(TransientProductState("none"), completed)
    assert completed_gate.actions == ()
    assert completed_gate.message == ""
    assert completed_panel.count("已合入 · Promotion receipt 已记录") == 1
    assert "任务已经到达 durable 终态" not in completed_panel
    assert "完成：Promotion receipt 已记录" not in completed_panel


def test_durable_usage_is_rendered_without_a_local_clock_estimate() -> None:
    observation = replace(
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
        ),
        usage=ProductUsage(
            tokens=34_840,
            token_quality=UsageQuality.EXACT,
            steps=2,
            wall_milliseconds=192_000,
        ),
    )

    rendered = []
    for now in (0.0, 99_999.0):
        rendered.append(
            product_panel_text(
                product_enabled=True,
                proposal=None,
                start_request=None,
                observation=observation,
                transient=TransientProductState("none"),
                now_monotonic=now,
                observation_received_at=0.0,
                operation_error=None,
                observation_error=None,
            )
        )
    usage_line = "用量   34840 tok · 2 步 · 用时 3 分 12 秒"
    assert all(usage_line in panel for panel in rendered)

    unavailable = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=replace(observation, usage=None),
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    assert "用量   — tok · — 步 · 用时 —" in unavailable
    assert "用量   0 tok" not in unavailable


def test_terminal_lifecycle_track_alone_is_muted() -> None:
    for status in (
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    ):
        panel = product_panel_text(
            product_enabled=True,
            proposal=None,
            start_request=None,
            observation=_observation(status, None),
            transient=TransientProductState("none"),
            now_monotonic=0.0,
            observation_received_at=None,
            operation_error=None,
            observation_error=None,
        )
        rendered = product_state_text(panel, terminal=True)
        assert len(rendered.spans) == 1
        span = rendered.spans[0]
        assert span.style == "dim"
        assert rendered.plain[span.start : span.end].startswith("进程内 ")
        assert "┊ durable " in rendered.plain[span.start : span.end]
        assert not (
            span.start
            <= rendered.plain.index("证据")
            < span.end
        )
        if status is not ProductTaskStatus.COMPLETED:
            assert "已合入 · Promotion receipt 已记录" not in rendered.plain

    active = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=_observation(
            ProductTaskStatus.STARTED,
            WorkflowStatus.RUNNING,
        ),
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    assert product_state_text(active, terminal=False).spans == []


def test_recent_fact_columns_are_fixed_and_product_role_prefix_is_shortened() -> None:
    role_session_id = "role-session"
    observation = replace(
        _observation(ProductTaskStatus.STARTED, WorkflowStatus.RUNNING),
        evidence=ProductTaskEvidence(
            "running",
            (
                ProductNodeEvidence(
                    "product-role-reviewer",
                    "agent",
                    "running",
                    "agent-reviewer",
                    role_session_id,
                    None,
                ),
            ),
            None,
        ),
        stream_heads=(
            ObservedStreamHead(
                "product-task:task-presentation",
                2,
                "product/task-started-with-an-excessively-long-suffix",
                NOW - timedelta(seconds=3),
                True,
            ),
            ObservedStreamHead(
                "workflow:task-presentation",
                4,
                "workflow/node-finished-with-an-excessively-long-suffix",
                NOW - timedelta(seconds=63),
                True,
            ),
            ObservedStreamHead(
                f"session:{role_session_id}",
                7,
                "turn/end-with-an-excessively-long-suffix",
                NOW - timedelta(seconds=303),
                True,
            ),
        ),
    )
    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    lines = rendered.splitlines()
    fact_start = lines.index("最近 durable 事实") + 1
    fact_lines = lines[fact_start : fact_start + 3]

    assert len(fact_lines) == 3
    # The wide pane has 52 usable columns after padding.  Stream/event stay
    # below their 18/22 caps, the two separators consume one column each, and
    # the age remains a fixed 12-column right-aligned field.
    assert all(_terminal_columns(line) == 52 for line in fact_lines)
    assert any("session·reviewer" in line for line in fact_lines)
    assert all("product-role-" not in line for line in fact_lines)
    assert all("…" in line for line in fact_lines)


def test_default_handle_is_short_generic_and_not_an_identity_alias() -> None:
    assert task_handle("修复库存预留并运行现有测试", "task-abcdef") == "修复库存预…"
    assert task_handle(None, "task-abcdef") == "ProductTask · task"


def test_narrow_projection_is_two_lines_plus_the_separate_gate_area() -> None:
    request = _start_request()
    rendered = product_compact_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=None,
        transient=TransientProductState("start_request"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    assert len(rendered.splitlines()) == 2
    assert "进程内 提议 ✓ · 确认 ✓ ┊ durable" in rendered
    assert "尚无任务事实" in rendered


def test_narrow_projection_keeps_host_errors_and_stalls_visible() -> None:
    request = _start_request()
    failed = product_compact_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=None,
        transient=TransientProductState("start_request"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=OperationErrorView("workspace-source-invalid", "guidance"),
        observation_error=None,
    )
    assert "宿主操作未完成 · workspace-source-invalid" in failed

    stalled = product_compact_text(
        product_enabled=True,
        proposal=request.pending,
        start_request=request,
        observation=None,
        transient=TransientProductState("operation_pending", "start", 25),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    assert "START 已等待 25 秒 · 无新任务事实" in stalled


def test_observation_failure_is_not_rendered_as_an_empty_task_list() -> None:
    error = OperationErrorView(
        "product-observation-unavailable",
        "durable facts remain unchanged",
    )
    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=None,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=error,
    )
    assert "ProductTask 状态暂不可读" in rendered
    assert "product-observation-unavailable" in rendered
    assert "宿主会按当前刷新周期重新读取" in rendered
    assert "当前：尚无提案" not in rendered


def test_global_streams_do_not_make_task_progress_look_recent() -> None:
    observation = replace(
        _observation(ProductTaskStatus.STARTED, WorkflowStatus.RUNNING),
        stream_heads=(
            ObservedStreamHead(
                "product-task:task-presentation",
                2,
                "product/task-started",
                NOW - timedelta(seconds=120),
                True,
            ),
            ObservedStreamHead(
                "agents:directory",
                99,
                "agent/created",
                NOW,
                False,
            ),
        ),
    )
    rendered = product_compact_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=0.0,
        operation_error=None,
        observation_error=None,
    )
    assert "最近任务事实 · 2 分 0 秒前" in rendered
    assert "最近任务事实 · 0 秒前" not in rendered


def test_failed_task_shows_verified_session_leaf_before_workflow_wrapper() -> None:
    observation = replace(
        _observation(ProductTaskStatus.FAILED, WorkflowStatus.FAILED),
        summary=replace(
            _summary(ProductTaskStatus.FAILED),
            failure_code="workflow-node-failed",
        ),
        evidence=ProductTaskEvidence(
            "failed",
            (
                ProductNodeEvidence(
                    "product-role-coder",
                    "agent",
                    "failed",
                    "agent-coder",
                    "session-coder",
                    "workflow-node-failed",
                    leaf_failure_code="provider-tool-arguments-invalid",
                    leaf_failure_category="protocol",
                ),
            ),
            None,
        ),
    )
    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    compact = product_compact_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )

    assert (
        "叶子失败：product-role-coder · provider-tool-arguments-invalid · protocol"
        in rendered
    )
    assert "任务已记录失败 · workflow-node-failed" in rendered
    assert "provider-tool-arguments-invalid" in compact


def test_failed_message_without_reliable_leaf_is_explicitly_unavailable() -> None:
    observation = replace(
        _observation(ProductTaskStatus.FAILED, WorkflowStatus.FAILED),
        summary=replace(
            _summary(ProductTaskStatus.FAILED),
            failure_code="workflow-node-failed",
        ),
        evidence=ProductTaskEvidence(
            "failed",
            (
                ProductNodeEvidence(
                    "product-role-coder",
                    "agent",
                    "failed",
                    "agent-coder",
                    "session-coder",
                    "workflow-agent-message-failed",
                ),
            ),
            None,
        ),
    )

    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    compact = product_compact_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )

    assert "叶子失败：product-role-coder · unavailable" in rendered
    assert "任务已记录失败 · workflow-node-failed" in rendered
    assert "unavailable" in compact


def test_review_evidence_renders_complete_file_summary_without_diff_body() -> None:
    changed_paths = ("src/core.py", "tests/test_core.py")
    preview_lines = (
        "diff --git a/src/core.py b/src/core.py",
        "index 1111111..2222222 100644",
        "--- a/src/core.py",
        "+++ b/src/core.py",
        "@@ -1 +1,2 @@",
        "+preview-body-must-not-render",
    )
    verifier_digest = "e" * 64
    patch_summary = UnifiedDiffSummary(
        files=(
            UnifiedDiffFileSummary(
                path=changed_paths[0],
                status="modified",
                additions=4,
                deletions=1,
                binary=False,
            ),
            UnifiedDiffFileSummary(
                path=changed_paths[1],
                status="added",
                additions=6,
                deletions=0,
                binary=False,
            ),
        ),
        additions=10,
        deletions=1,
        complete=True,
    )
    review_evidence = ProductReviewEvidence(
        changed_paths=changed_paths,
        patch_size_bytes=12_345,
        patch_preview="\n".join(preview_lines),
        patch_preview_truncated=True,
        patch_utf8_replaced=False,
        verifiers=(
            ProductVerifierEvidence(
                command_id="unit-tests",
                executable="python",
                argument_count=4,
                argv_digest=verifier_digest,
                status="passed",
                exit_code=0,
            ),
        ),
        patch_summary=patch_summary,
    )
    observation = replace(
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
            approval_ready=True,
        ),
        evidence=ProductTaskEvidence(
            WorkflowStatus.AWAITING_APPROVAL.value,
            (),
            review_evidence,
        ),
    )

    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )

    for path in changed_paths:
        assert path in rendered
    assert "unit-tests · passed · exit=0" in rendered
    assert verifier_digest[:12] in rendered
    assert "补丁    12345 bytes · 2 文件 · +10 −1" in rendered
    assert "src/core.py · 修改 · +4 −1" in rendered
    assert "tests/test_core.py · 新增 · +6 −0" in rendered
    for line in preview_lines:
        assert line not in rendered
    assert rendered.count("^p 查看完整身份") == 1
    assert rendered.count("^d 查看完整改动") == 1
    assert "预览已截断" not in rendered
    assert "本轮不提供" not in rendered
    lines = rendered.splitlines()
    separators = [
        index
        for index, line in enumerate(lines)
        if line.strip() and set(line.strip()) == {"─"}
    ]
    assert len(separators) == 3
    assert separators[0] < lines.index("最近 durable 事实")
    assert lines.index("最近 durable 事实") < separators[1]
    assert separators[1] < lines.index("证据") < separators[2]
    for heading in ("审批", "改动", "校验", "补丁"):
        assert heading in rendered
    for old_heading in (
        "Review / Approval",
        "Changed paths",
        "Verification",
        "Patch preview",
    ):
        assert old_heading not in rendered


def test_review_evidence_never_invents_counts_without_a_complete_summary() -> None:
    incomplete_summary = UnifiedDiffSummary(
        files=(
            UnifiedDiffFileSummary(
                path="src/module.py",
                status="unknown",
                additions=None,
                deletions=None,
                binary=False,
            ),
        ),
        additions=None,
        deletions=None,
        complete=False,
    )
    evidence = ProductReviewEvidence(
        changed_paths=("src/module.py",),
        patch_size_bytes=321,
        patch_preview="+a-line-that-must-not-be-counted",
        patch_preview_truncated=True,
        patch_utf8_replaced=False,
        verifiers=(),
        patch_summary=incomplete_summary,
    )
    observation = replace(
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
            approval_ready=True,
        ),
        evidence=ProductTaskEvidence(
            WorkflowStatus.AWAITING_APPROVAL.value,
            (),
            evidence,
        ),
    )

    rendered = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )

    assert "补丁    321 bytes · 1 文件 · +? −?" in rendered
    assert "src/module.py · 状态未知 · +? −?" in rendered
    assert "+a-line-that-must-not-be-counted" not in rendered


def test_full_identity_fields_are_exact_and_copy_keys_are_stable() -> None:
    task_id = "product-task-" + "1" * 64
    session_id = "session-" + "2" * 64
    review_id = "review-" + "3" * 64
    expected_revision = "4" * 40
    patch_digest = "5" * 64
    approval_digest = "6" * 64
    observation = replace(
        _observation(
            ProductTaskStatus.AWAITING_APPROVAL,
            WorkflowStatus.AWAITING_APPROVAL,
            approval_ready=True,
        ),
        task_id=task_id,
        summary=replace(
            _summary(ProductTaskStatus.AWAITING_APPROVAL),
            task_id=task_id,
            source_base_revision="7" * 40,
        ),
        review=SimpleNamespace(
            review_id=review_id,
            patch_sha256=patch_digest,
            target_ref="refs/heads/main",
            expected_revision=expected_revision,
        ),
        approval_digest=approval_digest,
    )

    fields = product_identity_fields(session_id, None, None, observation)
    copy_values = {
        field.copy_key: field.value
        for field in fields
        if field.copy_key is not None
    }

    assert copy_values == {
        "c": task_id,
        "s": session_id,
        "r": review_id,
        "t": f"refs/heads/main @ {expected_revision}",
        "p": patch_digest,
        "d": approval_digest,
    }
def test_presentation_does_not_advertise_unimplemented_reconcile_or_raw_event_keys() -> None:
    observation = _observation(
        ProductTaskStatus.STARTED,
        WorkflowStatus.AWAITING_APPROVAL,
    )
    panel = product_panel_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    compact = product_compact_text(
        product_enabled=True,
        proposal=None,
        start_request=None,
        observation=observation,
        transient=TransientProductState("none"),
        now_monotonic=0.0,
        observation_received_at=None,
        operation_error=None,
        observation_error=None,
    )
    rendered = "\n".join((panel, compact))

    for unsupported in ("^i", "Ctrl+I", "^r", "Ctrl+R"):
        assert unsupported not in rendered


def _terminal_columns(value: str) -> int:
    return sum(
        0
        if combining(character)
        else 2
        if east_asian_width(character) in {"W", "F"}
        else 1
        for character in value
    )
