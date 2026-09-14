"""Select real manifest cases and generate explicit background inputs without JSON editing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual import on
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static

from traceh.cli.tui_config import atomic_json
from traceh.evaluation.evaluators.episode_manifest import load_episode_suite
from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.tui.config_forms import background_preset, validate_document


class OptimizationPlanScreen(Screen):
    BINDINGS = [("escape", "back", "取消")]
    CSS = """
    OptimizationPlanScreen { padding: 1 2; }
    #optimization-cases { height: 12; }
    #optimization-plan-status { height: auto; color: $warning; }
    """

    def __init__(self, *, config_path, workspace, data_dir, model_settings, sandbox_config=""):
        super().__init__()
        self.config_path = Path(config_path).resolve()
        self.workspace, self.data_dir, self.model_settings = workspace, data_dir, model_settings
        self.manifest = None
        self.sandbox_config = sandbox_config

    def compose(self):
        with VerticalScroll():
            yield Label("后台优化入门 · 选题、填写额度，自动生成冻结计划")
            yield Label("检索／产品任务评估题库目录（包含 benchmark.json，不是你的工作区）")
            yield Input(id="optimization-benchmark")
            yield Button("读取可选题目", id="optimization-load-cases")
            yield SelectionList(id="optimization-cases")
            yield Label("材料版本（从题库实际存在的版本中选择）")
            yield Select([], prompt="先读取题库，再明确选择", id="optimization-seed")
            yield Label("产品任务沙箱配置（必填；隔离执行题库源码，检索题留空）")
            yield Input(self.sandbox_config, id="optimization-sandbox")
            yield Label("本周期最多尝试几份候选（不会自动采用）")
            yield Input("1", type="integer", id="optimization-episode-cap")
            yield Label("本周期有效几小时（到期不会自动续费）")
            yield Input("24", type="integer", id="optimization-hours")
            yield Label("候选至少净改善几题")
            yield Input("1", type="integer", id="optimization-gain")
            yield Label("任务 Token 最多为原版的几倍")
            yield Input("1.15", type="number", id="optimization-ratio")
            yield Static(
                "每道题跑原版与候选各一次。每份候选另预留分析 32,000 Token、"
                "每次裁判 64,000 Token；任务执行预算来自题库。最多增加 2 次工具调用。"
                "保存后可在完整表单修改这些显式预设。不会立即调用模型。",
                markup=False,
            )
            yield Button("保存选题与此周期额度", id="optimization-save-plan", variant="primary")
            yield Button("取消", id="optimization-plan-back")
            yield Static(
                "当前模型连接直接来自 F2 模型页，密钥不会写入计划。",
                id="optimization-plan-status",
                markup=False,
            )

    @on(Button.Pressed)
    def pressed(self, event):
        try:
            if event.button.id == "optimization-plan-back":
                self.dismiss(None)
            elif event.button.id == "optimization-load-cases":
                manifest = load_benchmark_manifest(
                    Path(self.query_one("#optimization-benchmark", Input).value).resolve()
                )
                if manifest.task_type.value == "product_task":
                    suite = load_product_suite(
                        manifest,
                        provider_id=self.model_settings["provider"],
                        model_id=self.model_settings["model"],
                    )
                    if suite.rubric is None or "multi" not in suite.modes:
                        raise ValueError("product-multi-semantic-benchmark-required")
                    names = {c.task_id: c.requirement[:80] for c in suite.tasks}
                    seeds = [("使用题库冻结源码（没有随机材料版本）", "source")]
                elif manifest.task_type.value == "retrieval_episode":
                    suite = load_episode_suite(manifest)
                    cases = [c.data for c in suite.cases if c.data["family"] != "output"]
                    names = {c["case_id"]: c["family"] for c in cases}
                    seeds = [(str(s), s) for s in sorted({c["material_seed"] for c in cases})]
                else:
                    raise ValueError("unsupported-task-type")
                choices = self.query_one("#optimization-cases", SelectionList)
                choices.clear_options()
                choices.add_options(
                    [(f"{key} ({family})", key, False) for key, family in sorted(names.items())]
                )
                self.query_one("#optimization-seed", Select).set_options(seeds)
                self.manifest = manifest
            elif event.button.id == "optimization-save-plan":
                self.save()
        except Exception:
            self.query_one("#optimization-plan-status", Static).update(
                "未保存：请检查题库、选题、材料版本、正数额度和模型连接。已有文件不会覆盖。"
            )

    def save(self):
        cases = sorted(self.query_one("#optimization-cases", SelectionList).selected)
        seed = self.query_one("#optimization-seed", Select).value
        episodes = int(self.query_one("#optimization-episode-cap", Input).value)
        hours = int(self.query_one("#optimization-hours", Input).value)
        gain = int(self.query_one("#optimization-gain", Input).value)
        ratio = float(self.query_one("#optimization-ratio", Input).value)
        if (
            not self.manifest
            or not cases
            or seed is Select.BLANK
            or episodes < 1
            or hours < 1
            or gain < 0
            or ratio < 0
            or self.model_settings["provider"] != "openai-compatible"
            or not all(self.model_settings[k] for k in ("model", "base_url", "api_key_env"))
        ):
            raise ValueError("explicit-selection-required")
        plan_path = self.config_path.with_name(self.config_path.stem + "-plan.json")
        if plan_path.exists() or self.config_path.exists():
            raise ValueError("use-new-config-destination")
        count = len(cases) * 2
        product = self.manifest.task_type.value == "product_task"
        sandbox = self.query_one("#optimization-sandbox", Input).value.strip()
        if product:
            from traceh.sandbox.config import load_sandbox_file

            if seed != "source" or not sandbox:
                raise ValueError("product-sandbox-required")
            sandbox = str(Path(sandbox).resolve())
            parsed = load_sandbox_file(Path(sandbox))
            if parsed.plugin_grants or parsed.policy.network != "none":
                raise ValueError("product-sandbox-scope-invalid")
        plan = {
            "format": 1,
            "benchmark_digest": self.manifest.document.sha256,
            "variants": [
                {"variant_id": "baseline", "role": "baseline", "source": "current"},
                {"variant_id": "candidate", "role": "candidate", "source": "current"},
            ],
            "model": {
                **self.model_settings,
                "script": None,
                "retry_policy": {
                    "max_attempts": 1,
                    "max_elapsed_seconds": 0,
                    "base_delay_seconds": 0,
                    "max_delay_seconds": 0,
                    "retry_after_cap_seconds": 0,
                    "jitter_ratio": 0,
                },
            },
            "execution": {
                "sandbox_config": sandbox if product else None,
                "max_trials": count,
                "timeout_seconds": 1800,
                "shutdown_seconds": 120,
                "first_arm": "baseline",
                "network_mode": "direct",
            },
            "trials": {
                "repetitions": 1,
                "selection": {"case_ids": cases, "material_seeds": None if product else [seed]},
            },
            "comparison": {
                "kind": "text_candidate",
                "requested_modes": ["multi", "multi"] if product else None,
                "format": 3,
                "min_pass_gain": gain,
                "max_token_ratio": ratio,
                "max_tool_call_delta": 2,
            },
        }
        raw = background_preset(self.workspace, self.data_dir)
        if product:
            from traceh.chat.background import DELEGATION_SELECTORS

            raw["selectors"] = [list(s) for s in DELEGATION_SELECTORS]
        raw.update(
            benchmark=str(self.manifest.directory),
            run_plan=str(plan_path),
            expires_at=(datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
            max_episodes=episodes,
            max_trials=episodes * count,
            max_control_tokens=episodes * (32000 + 64000 * count),
        )
        validate_document("background", raw, self.config_path)
        from traceh.evaluation.plan import comparison_policy

        comparison_policy(plan["comparison"])
        self.manifest.document.verify()
        self.manifest.dataset.verify()
        # Inputs are configuration, not execution. A failed second write leaves the
        # first plan visible; never remove or overwrite an existing user file.
        atomic_json(plan_path, plan)
        atomic_json(self.config_path, raw)
        self.dismiss(self.config_path)

    def action_back(self):
        self.dismiss(None)
