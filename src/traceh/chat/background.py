"""Chat assembly of explicitly configured background detection and suggestion (ADR-0082)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import object_fields, read_input
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_files
from traceh.evolution.background import (
    BackgroundOptimizationHost,
    BackgroundPeriod,
    pending_clusters,
)
from traceh.evolution.background_proposal import BackgroundProposal
from traceh.evolution.optimization import _inputs
from traceh.evolution.optimization_contract import editable_text
from traceh.sandbox.config import parse_sandbox_config

CODER_SELECTOR = ("product/execution.py", "CODER_GUIDANCE")
ALLOCATION_SELECTOR = ("supervision/structured_collaboration.py", "ALLOCATION_GUIDANCE")
PRODUCT_SELECTORS = (CODER_SELECTOR, ALLOCATION_SELECTOR)


@dataclass(frozen=True)
class BackgroundSettings:
    document: object
    benchmark: Path
    run_plan: Path
    output: Path
    workspace: Path
    expires_at: datetime


def load_background_settings(path):
    path = Path(path).resolve()
    doc = read_input(path.parent, path.name)
    return parse_background_settings(doc.data, path=path, document=doc)


def parse_background_settings(raw, *, path, document=None):
    object_fields(
        raw,
        {
            "format",
            "period_id",
            "workspace",
            "benchmark",
            "run_plan",
            "output",
            "expires_at",
            "max_episodes",
            "max_control_tokens",
            "max_observations",
            "cooldown_seconds",
            "suggestion_seconds",
            "max_request_bytes",
            "analysis",
            "selectors",
        },
        "background-settings",
    )
    if type(raw["format"]) is not int or raw["format"] != 2:
        raise ValueError("background-settings-version-unsupported")
    for key in ("period_id", "workspace", "benchmark", "run_plan", "output", "expires_at"):
        if type(raw[key]) is not str or not raw[key].strip():
            raise ValueError("background-settings-required-field")
    for key in (
        "max_episodes",
        "max_control_tokens",
        "max_observations",
        "cooldown_seconds",
        "suggestion_seconds",
        "max_request_bytes",
    ):
        if type(raw[key]) is not int or raw[key] < 1:
            raise ValueError("background-settings-positive-limit-required")
    expires = datetime.fromisoformat(raw["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("background-settings-timezone-required")
    expires = expires.astimezone(UTC)
    config = object_fields(
        raw["analysis"],
        {"encoding", "token_limit", "output_tokens", "safety_tokens", "timeout_seconds"},
        "background-control-model",
    )
    ModelCallConfig(
        "validation-only",
        "validation-only",
        0.0,
        **config,
        connection_digest=fingerprint("validation-only"),
    )
    if (
        type(raw["selectors"]) is not list
        or not raw["selectors"]
        or any(type(s) is not list or len(s) != 2 for s in raw["selectors"])
    ):
        raise ValueError("background-settings-selectors-invalid")
    editable_text(source_files()[1], tuple(tuple(s) for s in raw["selectors"]))
    root = Path(path).resolve().parent
    paths = {k: (root / raw[k]).resolve() for k in ("benchmark", "run_plan", "output", "workspace")}
    if paths["output"].is_relative_to(paths["benchmark"]):
        raise ValueError("background-output-overlaps-benchmark")
    return BackgroundSettings(document, **paths, expires_at=expires)


def assemble_background(runtime, settings, *, provider, model, base_url, api_key=None):
    """Bind the same provider identity; the benchmark only scopes and binds suggestions."""
    settings.document.verify()
    raw = settings.document.data
    options = load_run_options(settings.run_plan)
    model_settings = options.document.data["model"]
    if (
        model_settings["provider"] != provider.name
        or model_settings["model"] != model
        or model_settings["base_url"] != base_url
    ):
        raise ValueError("background-model-or-evaluation-target-invalid")
    sandbox_path = options.document.data["execution"]["sandbox_config"]
    sandbox = None
    if sandbox_path is not None:
        path = (settings.run_plan.parent / sandbox_path).resolve()
        sandbox_document = read_input(path.parent, path.name)
        parsed = parse_sandbox_config(sandbox_document.data)
        if parsed.plugin_grants or parsed.policy.network != "none":
            raise ValueError("background-sandbox-scope-invalid")
        sandbox = parsed.policy
    connection = fingerprint(model_settings["base_url"])
    template = EvaluationRunner(
        settings.benchmark,
        settings.output / "unexecuted-template",
        provider=provider,
        model_id=model,
        options=options,
        sandbox=sandbox,
        worker_api_key=api_key,
        provider_binding={"connection_digest": connection, "purpose": "background-optimization"},
    )
    selectors = tuple(tuple(s) for s in raw["selectors"])
    task_type = template.manifest.task_type.value
    if task_type == "product_task":
        modes = options.document.data["comparison"]["requested_modes"]
        if (
            sandbox is None
            or not set(selectors) <= set(PRODUCT_SELECTORS)
            or (ALLOCATION_SELECTOR in selectors and modes != ["multi", "multi"])
        ):
            raise ValueError("background-product-contract-invalid")
    elif task_type != "retrieval_episode" or sandbox is not None:
        raise ValueError("background-task-type-not-enabled")
    _inputs(template)
    analysis = ModelCallConfig(
        provider.name, model, 0.0, **raw["analysis"], connection_digest=connection
    )
    proposal = BackgroundProposal(
        template,
        provider=provider,
        analysis_config=analysis,
        output=settings.output,
        selectors=selectors,
        timeout_seconds=raw["suggestion_seconds"],
        max_request_bytes=raw["max_request_bytes"],
    )
    period = BackgroundPeriod(
        raw["period_id"],
        str(settings.workspace),
        proposal.baseline,
        fingerprint((settings.document.sha256, options.document.sha256)),
        settings.expires_at,
        *(
            raw[k]
            for k in ("max_episodes", "max_control_tokens", "max_observations", "cooldown_seconds")
        ),
    )
    if proposal.reservation.control_tokens > period.max_control_tokens:
        raise ValueError("background-period-cannot-reserve-one-suggestion")
    return BackgroundOptimizationHost(
        runtime.sessions,
        period,
        reservation=proposal.reservation,
        execute=proposal,
        scope={
            "benchmark_digest": template.manifest.document.sha256,
            "case_ids": frozenset(proposal.case_ids),
        },
    )


async def submit_background_feedback(host, session_id, text):
    events = await host.sessions.read_session(session_id)
    ended = next((e for e in reversed(events) if e.type == "turn/end"), None)
    if ended is None:
        raise ValueError("background-no-completed-turn")
    return await host.observe(session_id, ended.data["turn_id"], feedback=text)


def background_status(state):
    if state["blocked"]:
        status = f"已停止：建议调用的成本或收尾未核实（{state['blocked']}），核实后可解除"
    elif state["stale"]:
        status = "源码或设置已变化：批准一个新周期后才会继续"
    elif state["active"]:
        status = "正在根据反复出现的问题生成建议（另一宿主遗留的未结算建议也会占用此位置）"
    elif state["pending_review"]:
        status = "有一条建议等待你查看；不会自动采用，也不会自动评测"
    elif state["enabled"]:
        status = "正在检测；同一类问题出现在至少两处来源才会生成建议"
    else:
        status = "已暂停"
    waiting = ", ".join(
        f"{name}×{len(entry['sources'])}" for name, entry in sorted(pending_clusters(state).items())
    )
    return (
        f"后台建议：{status}\n"
        f"本周期上限：{state['period']['max_episodes']} 次建议；"
        f"到期：{state['period']['expires_at']}\n"
        f"已预留 {state['episodes']} 次建议，分析 Token 上限合计 {state['control_tokens']}"
        "（不是实际消耗）。\n"
        f"待聚类的问题（类别×来源数）：{waiting or '无'}\n"
        f"最近一次建议原件：{state['last_evidence'] or '尚无'}\n"
        "检测只统计原事件中的结构信号，不代表答案错误；建议是否有效需要你用评测验证。"
    )


def background_proposal_text(path):
    """Read a settled original suggestion and render it in plain language."""
    from traceh.evolution.strategy import inspect_strategy_optimization

    root = Path(path)
    result = inspect_strategy_optimization(root)
    analysis = result["cost"]["analysis"]
    lines = [
        "最近一次建议（从原证据重新核对）",
        f"分析 Token：{analysis['total_tokens'] if analysis else '未知'}。",
        "没有自动采用或评测任何修改。",
    ]
    candidate = result["candidate"]
    if candidate is None:
        lines.append(f"没有产生可用建议（{result['reason']}）。")
        return "\n".join(lines)
    proposal = read_input(root, "proposal.json").data
    lines.extend(
        (
            "",
            "针对的问题：" + "、".join(proposal["targeted_failure_classes"]),
            "理由：" + proposal["rationale"],
            "预期代价：" + proposal["expected_tradeoffs"],
            "建议文本：",
        )
    )
    for edit in proposal["edits"]:
        lines.extend((f"{edit['file']} :: {edit['selector']}", edit["new_text"], ""))
    lines.append(
        "如需验证：把运行计划中 candidate 变体的 source 指向 "
        f"{root / candidate['file']}，再运行 traceh eval。"
    )
    return "\n".join(lines)
