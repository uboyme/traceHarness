"""Bounded, markup-inert text projections used by the Textual adapter."""

from __future__ import annotations

from traceh.cli.command_line import escape_for_display
from traceh.product.observation import ProductObservation

MAX_BLOCK_CHARS = 12_000
MAX_BLOCK_LINES = 160
MAX_LINE_CHARS = 800


def safe_display_block(
    value: object,
    *,
    limit: int = MAX_BLOCK_CHARS,
    max_lines: int = MAX_BLOCK_LINES,
) -> str:
    """Render an untrusted value as bounded plain text.

    Literal LF is the only preserved separator. Every line independently goes
    through the existing terminal safety rule, so CR, ESC, bidi controls,
    binary replacement characters and Textual/Rich-looking markup remain inert.
    """

    text = value if type(value) is str else str(value)
    lines = text.split("\n")
    truncated_lines = len(lines) > max_lines
    rendered = [
        escape_for_display(line, limit=MAX_LINE_CHARS)
        for line in lines[:max_lines]
    ]
    if truncated_lines:
        rendered.append("… (more lines omitted)")
    block = "\n".join(rendered)
    if len(block) <= limit:
        return block
    return block[: limit - 1] + "…"


def product_observation_text(observation: ProductObservation) -> str:
    """Project one fresh Product observation into a compact review panel."""

    summary = observation.summary
    if summary is None:
        return "ProductTask unavailable: the task stream has no opening fact."
    mode = "pending" if summary.resolved_mode is None else summary.resolved_mode.value
    workflow = (
        "not-started"
        if observation.workflow_status is None
        else observation.workflow_status.value
    )
    lines = [
        f"Task: {summary.task_id}",
        f"Product: {summary.status.value}",
        f"Workflow: {workflow}",
        (
            f"Mode: requested={summary.requested_mode.value}; "
            f"source={summary.mode_source.value}; resolved={mode}"
        ),
    ]
    if observation.streams_diverged:
        lines.append("Evidence: Product/Workflow streams are not yet reconciled")
    evidence = observation.evidence
    if evidence is not None and evidence.nodes:
        lines.append("")
        lines.append("Fixed Workflow")
        for node in evidence.nodes:
            detail = f"  {node.node_id}: {node.status} ({node.kind})"
            if node.failure_code is not None:
                detail += f" failure={node.failure_code}"
            lines.append(detail)
    review = observation.review
    if review is not None:
        lines.extend(
            (
                "",
                "Review / Approval",
                f"  review: {review.review_id}",
                f"  patch_sha256: {review.patch_sha256}",
                f"  target: {review.target_ref} at {review.expected_revision}",
                f"  integration_commit: {review.integration_commit}",
                f"  approval_digest: {observation.approval_digest}",
            )
        )
    if evidence is not None and evidence.review is not None:
        report = evidence.review
        lines.append(f"  changed paths ({len(report.changed_paths)}):")
        lines.extend(f"    {path}" for path in report.changed_paths)
        lines.append("  verification:")
        for verifier in report.verifiers:
            exit_code = (
                "unavailable" if verifier.exit_code is None else str(verifier.exit_code)
            )
            lines.append(
                f"    {verifier.command_id}: {verifier.status} exit={exit_code}; "
                f"argv_sha256={verifier.argv_digest}"
            )
        suffix = "truncated" if report.patch_preview_truncated else "complete"
        lines.append(f"  patch preview ({report.patch_size_bytes} bytes, {suffix}):")
        lines.extend(f"    {line}" for line in report.patch_preview.split("\n"))
        if report.patch_utf8_replaced:
            lines.append("    non-UTF-8 bytes are shown with replacement characters")
    if observation.approval is not None:
        lines.append(f"Approval recorded: {observation.approval.approval_id}")
    if observation.promotion is not None:
        lines.append(f"Promotion recorded: {observation.promotion.promotion_id}")
    return safe_display_block("\n".join(lines))


__all__ = [
    "MAX_BLOCK_CHARS",
    "MAX_BLOCK_LINES",
    "MAX_LINE_CHARS",
    "product_observation_text",
    "safe_display_block",
]
