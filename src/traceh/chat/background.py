"""Chat assembly of explicitly configured background optimization, outside Runtime."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import object_fields, read_input
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_files
from traceh.evolution.background import BackgroundOptimizationHost, BackgroundPeriod
from traceh.evolution.background_experiment import BackgroundExperiment
from traceh.evolution.optimization import _inputs
from traceh.evolution.optimization_contract import editable_text


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
            "max_trials",
            "max_control_tokens",
            "max_observations",
            "cooldown_seconds",
            "episode_seconds",
            "max_request_bytes",
            "analysis",
            "judge",
            "selectors",
        },
        "background-settings",
    )
    if type(raw["format"]) is not int or raw["format"] != 1:
        raise ValueError("background-settings-version-unsupported")
    for key in ("period_id", "workspace", "benchmark", "run_plan", "output", "expires_at"):
        if type(raw[key]) is not str or not raw[key].strip():
            raise ValueError("background-settings-required-field")
    for key in (
        "max_episodes",
        "max_trials",
        "max_control_tokens",
        "max_observations",
        "cooldown_seconds",
        "episode_seconds",
        "max_request_bytes",
    ):
        if type(raw[key]) is not int or raw[key] < 1:
            raise ValueError("background-settings-positive-limit-required")
    expires = datetime.fromisoformat(raw["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("background-settings-timezone-required")
    expires = expires.astimezone(UTC)
    for key in ("analysis", "judge"):
        config = object_fields(
            raw[key],
            {
                "encoding",
                "token_limit",
                "output_tokens",
                "safety_tokens",
                "timeout_seconds",
            },
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
    """Bind the same provider identity; user trial source and scoring stay frozen."""
    settings.document.verify()
    raw = settings.document.data
    options = load_run_options(settings.run_plan)
    model_settings = options.document.data["model"]
    if (
        model_settings["provider"] != provider.name
        or model_settings["model"] != model
        or model_settings["base_url"] != base_url
        or options.document.data["execution"]["sandbox_config"] is not None
    ):
        raise ValueError("background-model-or-evaluation-target-invalid")
    # Initial AO-3 accepts source-isolated retrieval; no user code/command replay.
    connection = fingerprint(model_settings["base_url"])
    template = EvaluationRunner(
        settings.benchmark,
        settings.output / "unexecuted-template",
        provider=provider,
        model_id=model,
        options=options,
        worker_api_key=api_key,
        provider_binding={"connection_digest": connection, "purpose": "background-optimization"},
    )
    if template.manifest.task_type.value != "retrieval_episode":
        raise ValueError("background-task-type-not-enabled")
    _inputs(template)
    configs = {
        k: ModelCallConfig(provider.name, model, 0.0, **raw[k], connection_digest=connection)
        for k in ("analysis", "judge")
    }
    experiment = BackgroundExperiment(
        template,
        provider=provider,
        analysis_config=configs["analysis"],
        judge_config=configs["judge"],
        output=settings.output,
        selectors=tuple(tuple(s) for s in raw["selectors"]),
        timeout_seconds=raw["episode_seconds"],
        max_request_bytes=raw["max_request_bytes"],
    )
    period = BackgroundPeriod(
        raw["period_id"],
        str(settings.workspace),
        experiment.baseline,
        fingerprint((settings.document.sha256, options.document.sha256)),
        settings.expires_at,
        *(
            raw[k]
            for k in (
                "max_episodes",
                "max_trials",
                "max_control_tokens",
                "max_observations",
                "cooldown_seconds",
            )
        ),
    )
    if (
        experiment.reservation.trials > period.max_trials
        or experiment.reservation.control_tokens > period.max_control_tokens
    ):
        raise ValueError("background-period-cannot-reserve-one-experiment")
    return BackgroundOptimizationHost(
        runtime.sessions,
        period,
        reservation=experiment.reservation,
        execute=experiment,
    )


async def submit_background_feedback(host, session_id, text):
    events = await host.sessions.read_session(session_id)
    ended = next((e for e in reversed(events) if e.type == "turn/end"), None)
    if ended is None:
        raise ValueError("background-no-completed-turn")
    return await host.observe(session_id, ended.data["turn_id"], feedback=text)


def background_status(state):
    if state["blocked"]:
        status = "已停止：实验成本或收敛尚未核实"
    elif state["active"]:
        status = "正在分析／验证（另一宿主遗留的未结算实验也会占用此位置）"
    elif state["pending_review"]:
        status = "等待审阅；不会自动采用"
    elif state["enabled"]:
        status = "等待新反馈／冷却／额度检查"
    else:
        status = "已暂停"
    return (
        f"后台优化：{status}\n"
        f"本周期上限：{state['period']['max_episodes']} 次实验、"
        f"{state['period']['max_trials']} 次任务；到期：{state['period']['expires_at']}\n"
        f"已预留 {state['episodes']} 次实验、{state['trials']} 次完整任务，"
        f"分析与裁判 Token 上限合计 {state['control_tokens']}（不是实际消耗）。\n"
        f"原评估证据：{state['last_evidence'] or '尚无'}\n"
        "反馈不等于标准答案；实际成本和成绩以原实验报告为准。"
    )


def background_experiment_text(path):
    """Read a settled original experiment and render its comparison in plain language."""
    from traceh.evolution.strategy import inspect_strategy_optimization

    root = Path(path)
    result = inspect_strategy_optimization(root)
    analysis = result["cost"]["analysis"]
    lines = [
        "最近一次实验（从原证据重新核对）",
        f"分析 Token：{analysis['total_tokens'] if analysis else '未知'}；"
        f"裁判 Token：{result['cost']['review_tokens']}。",
        "没有自动采用任何修改。",
    ]
    if result["evaluation"]:
        for row in result["evaluation"]["rounds"]:
            comparison = row["comparison"]
            if comparison is None or comparison["status"] == "not_comparable":
                lines.append("评估证据尚不完整，不能判定优劣。")
                continue
            changes = comparison["changes"]
            lines.append(
                f"按当前评分：改善 {changes['gain']} 题，退步 {changes['loss']} 题，"
                f"不变 {changes['unchanged']} 题，未知 {changes['unknown']} 题。"
            )
            lines.append(
                "原版／候选任务 Token："
                + " / ".join(str(a["cost"]["total_tokens"]) for a in comparison["arms"])
            )
            if row["action"] == "review_candidate":
                lines.append("达到开发比较门槛，可以交用户审阅；不保证泛化改善。")
            elif row["action"] == "await_review":
                lines.append("还有语义判断待审，暂时不能判胜。")
            else:
                lines.append("本轮未达到晋级条件，保留当前版本。")
    else:
        lines.append("没有进入候选比较：可能没有可支持的修改，或提案阶段已停止。")
    if (root / "proposal.json").exists():
        proposal = read_input(root, "proposal.json").data
        if proposal.get("edits"):
            lines.extend(("", "候选理由：" + proposal["rationale"], "候选说明文本："))
            for edit in proposal["edits"]:
                lines.extend((f"{edit['file']} :: {edit['selector']}", edit["new_text"], ""))
    return "\n".join(lines)
