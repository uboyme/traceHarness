"""Launch settings presentation. Editing never mutates an active Runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from traceh.chat.config import load_context_host_file, parse_context_host_config
from traceh.cli.tui_config import (
    FIELDS,
    LaunchConfigurationError,
    apply_values,
    atomic_json,
    load_profile,
    preflight,
    save_profile,
)
from traceh.cli.tui_entry import save_personal_profile

_LABELS = {
    "token_encoding": "本地计数编码（tiktoken 编码名；留空关闭，结果标为估算）",
    "context_window_tokens": "当前模型的上下文窗口（token，按服务商说明填写）",
    "context_output_reserve": "为本次回答预留多少 token（同时限制最大输出）",
    "context_safety_margin": "估算误差安全余量（token，不可用于输入和回答）",
    "context_trigger_percent": "达到输入预算多少百分比时提前压缩（1–100，留空为 80）",
    "workspace": "工作区目录（新会话，与会话 ID 二选一）",
    "session_id": "继续已有会话 ID（继续时清空工作区）",
    "provider": "模型接入方式（兼容接口填 openai-compatible；离线测试填 scripted）",
    "model": "模型名称（填写服务商提供的名称）",
    "base_url": "模型服务地址（HTTP(S) 接口根地址，可带 /v1）",
    "api_key_env": "从哪个环境变量取密钥（高级；直接填下方密码框也可以）",
    "env_file": "环境文件路径（只由原有加载器读取，不展示或改写内容）",
    "data_dir": "数据目录（事件账本存放处；留空沿用 CLI 默认）",
    "max_steps": "每轮最大步数（留空沿用 CLI 默认）",
    "plugins": "启用的已安装插件 ID（空格分隔；清空表示不启用外部插件）",
    "context_config": "Context 配置 JSON 路径（Skill、Memory、History）",
    "product_config": "Product 配置 JSON 路径（可选）",
    "sandbox_config": "沙箱配置文件路径（可选；关闭时禁止执行进程）",
    "background_config": "后台优化配置文件（可选；使用下方表单编辑）",
    "script": "Scripted 响应文件路径（可选，仅 scripted 使用）",
    "auto_compact_bytes": "触发大小（UTF-8 字节；这是历史大小，不是 token 数）",
    "auto_compact_summary_bytes": "压缩后摘要最多多少字节",
    "auto_compact_keep_turns": "保留最近多少轮完整对话（0 表示不额外保留）",
    "default_project_id": "此工作区已确认的默认项目（清空停止自动关联）",
    "project_actor_id": "自动关联使用的操作署名",
    "project_workspace": "默认项目选择所属工作区（精确目录）",
}


class SettingsScreen(Screen[argparse.Namespace | None]):
    BINDINGS = [Binding("escape", "back", "返回", priority=True)]
    CSS = """
    SettingsScreen { layout: vertical; }
    #settings-title { height: auto; padding: 1 2; background: $panel; }
    #settings-tabs { height: 1fr; }
    .settings-fields { padding: 0 2; }
    .settings-fields Label { margin-top: 1; height: auto; }
    .settings-fields Input { width: 100%; }
    #settings-status { height: auto; max-height: 4; padding: 0 2; color: $warning; }
    #settings-actions { height: 3; padding: 0 2; }
    #settings-actions Button { margin-right: 1; min-width: 12; }
    #context-editor { height: 20; min-height: 8; }
    #context-actions { height: 3; }
    #context-actions Button { margin-right: 1; }
    #installed-plugins { height: 8; }
    """

    def __init__(
        self, args, values, profile_path: Path, *, startup: bool, initial_error="", live_apply=None
    ):
        super().__init__()
        self._args = args
        self._values = dict(values)
        self._profile_path = profile_path
        self._startup = startup
        self._live_apply = live_apply
        self._keep_key = True
        self._initial_error = initial_error
        self._loaded_context_path: Path | None = None
        self._loaded_context_bytes: bytes | None = None

    def _fields(self, names) -> ComposeResult:
        for name in names:
            yield Label(_LABELS[name])
            yield Input(self._values.get(name, ""), id=f"setting-{name}")

    def compose(self) -> ComposeResult:
        yield Static(
            "启动配置 · 已保存的值会自动带入。按分类查看、修改或关闭功能，再点击「启动聊天」。"
            if self._startup
            else "运行配置 · 应用后收尾旧运行环境并恢复会话；新工作区请清空会话 ID。",
            id="settings-title",
            markup=False,
        )
        with TabbedContent(id="settings-tabs"):
            with TabPane("模型连接", id="settings-model"):
                with VerticalScroll(classes="settings-fields"):
                    yield from self._fields(("provider", "model", "base_url"))
                    if self._startup or self._live_apply is not None:
                        yield Label(
                            "临时 API Key（不保存；同地址留空保留当前密钥，更换地址需重新填）"
                        )
                        yield Input(password=True, id="settings-key")
                    with Collapsible(title="高级：密钥变量与环境文件", collapsed=True):
                        yield from self._fields(("api_key_env", "env_file"))
            with TabPane("Token 预算", id="settings-tokens"):
                with VerticalScroll(classes="settings-fields"):
                    yield Static(
                        "同时计量系统提示、工具定义、任务状态、对话和本轮参考资料。"
                        "本地编码不一定等于远端模型编码，因此显示估算，服务返回的实际用量另列。"
                        "四项留空关闭。启用时需安装 tokens 可选依赖；"
                        "首次加载编码可能下载公开词表。",
                        markup=False,
                    )
                    yield from self._fields(
                        (
                            "token_encoding",
                            "context_window_tokens",
                            "context_output_reserve",
                            "context_safety_margin",
                            "context_trigger_percent",
                        )
                    )
                    yield Static(
                        "若自动压缩也已开启，首轮请求按完整 token 预算压缩旧历史；"
                        "关闭自动压缩只计量并拒绝超限请求。活动工具组不拆分。",
                        markup=False,
                    )
            with TabPane("会话与运行", id="settings-runtime"):
                with VerticalScroll(classes="settings-fields"):
                    yield Static(
                        "当前文件夹就是工作区。聊天中按 Ctrl+O 选择历史对话，"
                        "输入 /new 开始新对话，无需填写 ID。"
                    )
                    with Collapsible(title="高级：工作区、会话与存储位置", collapsed=True):
                        yield from self._fields(
                            (
                                "workspace",
                                "session_id",
                                "data_dir",
                                "default_project_id",
                                "project_actor_id",
                                "project_workspace",
                            )
                        )
                    yield from self._fields(("max_steps", "plugins", "script"))
                    yield Label("启用所选插件（关闭不卸载）")
                    yield Switch(bool(self._values.get("plugins")), id="plugins-enabled")
                    with Collapsible(title="查看并勾选本机已安装插件", collapsed=True):
                        from traceh.plugins.discovery import PluginDiscovery

                        selected = self._values.get("plugins", "").split()
                        records = [r for r in PluginDiscovery().discover() if not r.issues]
                        yield SelectionList(
                            *[
                                (r.entry_name, r.entry_name, r.entry_name in selected)
                                for r in records
                            ],
                            id="installed-plugins",
                        )
                        yield Button("使用勾选插件", id="plugins-use")
                    yield Static(
                        "插件只启用本机已安装项；选 Skill、绑定项目、审批 Memory "
                        "仍走原有治理命令。",
                        markup=False,
                    )
            with TabPane("知识与记忆", id="settings-context"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("启用知识与记忆（Skill、项目 Memory、历史原文读取）")
                    yield Switch(bool(self._values.get("context_config")), id="context-enabled")
                    yield Button("配置知识与记忆", id="context-form")
                    yield Static(
                        "点击上方按钮，在中文分组里选择要开启的功能、填写来源和预算。"
                        "启用读取能力不会自动选择 Skill、绑定项目或批准记忆。",
                        markup=False,
                    )
                    with Collapsible(
                        title="高级：文件路径与原始 JSON", collapsed=True, id="context-advanced"
                    ):
                        yield from self._fields(("context_config",))
                        with Horizontal(id="context-actions"):
                            yield Button("加载 Context", id="context-load")
                            yield Button("校验并保存 Context", id="context-save")
                        yield TextArea("", id="context-editor", soft_wrap=True)
            with TabPane("任务执行", id="settings-product"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("启用 ProductTask（隔离工作区、验证、人工审批）")
                    yield Switch(bool(self._values.get("product_config")), id="product-enabled")
                    yield Button("配置任务执行", id="product-form")
                    yield Static(
                        "填写 Git 项目、模型、审批署名、验证命令和结果接收仓库。"
                        "配置时不执行任务、不创建 Git 仓库、不批准结果；"
                        "当前会话装配变化仍由原规则检查。",
                        markup=False,
                    )
                    with Collapsible(title="高级：已有任务配置文件", collapsed=True):
                        yield from self._fields(("product_config",))
            with TabPane("执行沙箱", id="settings-sandbox"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("启用 Docker 执行沙箱（工具命令与任务验证共用）")
                    yield Switch(bool(self._values.get("sandbox_config")), id="sandbox-enabled")
                    yield Button("配置执行沙箱", id="sandbox-form")
                    yield Static(
                        "Docker 连接和运行环境可刷新下拉选择，也可手动填写名称或镜像 ID。"
                        "系统解析并保存固定身份，不需要手动复制 hash。"
                        "命令在隔离副本中运行，网络关闭；工具改动经检查后回写授权目录，"
                        "验证命令的改动不回写。关闭后进程命令会被拒绝。"
                        "刷新和镜像校验只查询 Docker，不启动服务、不下载镜像、不执行任务。",
                        markup=False,
                    )
                    with Collapsible(title="高级：已有沙箱配置文件", collapsed=True):
                        yield from self._fields(("sandbox_config",))
            with TabPane("后台优化", id="settings-background"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("装配后台受限优化（进入聊天后按 F6 显式开启）")
                    yield Switch(
                        bool(self._values.get("background_config")), id="background-enabled"
                    )
                    yield Button("配置后台优化", id="background-form")
                    yield Button("新建：选题并生成评估计划", id="background-plan")
                    yield Static(
                        "选择冻结评估题库和双臂计划，填写本周期额度与到期时间。"
                        "后台只改获准说明文本、隔离验证，不自动采用；反馈不等于标准答案。",
                        markup=False,
                    )
                    yield from self._fields(("background_config",))
            with TabPane("自动压缩", id="settings-compaction"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("旧对话越来越长时，是否自动腾出上下文空间")
                    yield Select(
                        [
                            ("沿用启动参数或环境配置", ""),
                            ("开启自动压缩", "on"),
                            ("关闭自动压缩", "off"),
                        ],
                        value=self._values.get("auto_compact", ""),
                        allow_blank=False,
                        id="setting-auto_compact",
                    )
                    yield Static(
                        "先折叠旧工具结果，仍太大才按所选方式摘要。开启后必须填写下方三项；"
                        "模型摘要使用当前连接，会消耗 token，需配置 Token 预算且至少允许 2 步。"
                        "仅处理闭合旧历史，保留原事件；摘要失败不伪造成功。",
                        markup=False,
                    )
                    yield Label("旧历史的摘要方式")
                    yield Select(
                        [
                            ("规则摘录（不调用模型）", "extractive"),
                            ("模型语义摘要（使用当前连接）", "semantic"),
                            ("默认规则摘录", ""),
                        ],
                        value=self._values.get("auto_compact_method", ""),
                        allow_blank=False,
                        id="setting-auto_compact_method",
                    )
                    yield from self._fields(
                        (
                            "auto_compact_bytes",
                            "auto_compact_summary_bytes",
                            "auto_compact_keep_turns",
                        )
                    )
            with TabPane("保存配置", id="settings-profile"):
                with VerticalScroll(classes="settings-fields"):
                    yield Label("启动配置文件（非密钥 JSON；保存不会保存临时 API Key）")
                    yield Input(str(self._profile_path), id="settings-profile-path")
                    yield Button("加载启动配置", id="settings-load")
                    yield Button("保存为个人默认", id="settings-save-personal")
                    yield Static(
                        "运行 traceh 自动加载个人默认和当前目录的 .traceh-tui.json。"
                        "个人默认只保存模型连接、环境文件和最大步数，不绑定工作区或会话。"
                        "保存包含本面板字段和压缩选项；配置完整时下次直接进入聊天。",
                        markup=False,
                    )
        yield Static(self._initial_error, id="settings-status", markup=False)
        with Horizontal(id="settings-actions"):
            yield Button("本地校验", id="settings-check")
            yield Button("保存配置", id="settings-save")
            if self._startup:
                yield Button("启动聊天", id="settings-start", variant="primary")
            elif self._live_apply is not None:
                yield Button("应用配置", id="settings-start", variant="primary")
            yield Button("取消" if self._startup else "返回聊天", id="settings-back")
        yield Footer()

    def _status(self, text: str) -> None:
        self.query_one("#settings-status", Static).update(text)

    def _draft(self):
        values = {name: self.query_one(f"#setting-{name}").value for name in FIELDS}
        if not self.query_one("#plugins-enabled", Switch).value:
            values["plugins"] = ""
        for kind in ("context", "product", "sandbox", "background"):
            if not self.query_one(f"#{kind}-enabled", Switch).value:
                values[f"{kind}_config"] = ""
            elif not values[f"{kind}_config"].strip():
                raise LaunchConfigurationError("请先点击配置按钮，填写并保存要启用的功能。")
        args = apply_values(self._args, values)
        if args.env_file != getattr(self._args, "env_file", None) or not self._keep_key:
            args._personal_env_only = False
        if self._startup or self._live_apply is not None:
            same_endpoint = all(
                getattr(args, name, None) == getattr(self._args, name, None)
                for name in ("provider", "base_url", "api_key_env")
            )
            existing = getattr(self._args, "tui_api_key", None)
            args.tui_api_key = self.query_one("#settings-key", Input).value or (
                existing if same_endpoint and self._keep_key else None
            )
        return args, values

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "settings-back":
            self.action_back()
            return
        try:
            if action == "background-plan":
                self._open_background_plan()
                return
            if action == "plugins-use":
                selected = self.query_one("#installed-plugins", SelectionList).selected
                self.query_one("#setting-plugins", Input).value = " ".join(selected)
                self.query_one("#plugins-enabled", Switch).value = bool(selected)
                return
            if action == "settings-load":
                path = Path(self.query_one("#settings-profile-path", Input).value).expanduser()
                values = load_profile(path)
                for name, value in values.items():
                    self.query_one(f"#setting-{name}").value = value
                for kind in ("context", "product", "sandbox", "background"):
                    self.query_one(f"#{kind}-enabled", Switch).value = bool(
                        values[f"{kind}_config"]
                    )
                if self._startup or self._live_apply is not None:
                    self.query_one("#settings-key", Input).value = ""
                self._keep_key = False
                self.query_one("#context-editor", TextArea).load_text("")
                self._loaded_context_path = self._loaded_context_bytes = None
                self._status("已加载启动配置；尚未生效。临时密钥和 Context 编辑草稿已清空。")
                return
            if action in {"context-load", "context-save"}:
                self._context_action(action)
                return
            if action in {"context-form", "product-form", "sandbox-form", "background-form"}:
                self._open_form(action.split("-")[0])
                return
            args, values = self._draft()
            if action in {"settings-check", "settings-start"}:
                result = preflight(args)
                if self._live_apply is not None:
                    self._live_apply(args)
                self._status(result)
                if action == "settings-start":
                    args.tui_profile = (
                        Path(self.query_one("#settings-profile-path", Input).value)
                        .expanduser()
                        .resolve()
                    )
                    self.query_one("#settings-key", Input).value = ""
                    self.dismiss(args)
            elif action == "settings-save-personal":
                save_personal_profile(values)
                self._status("个人默认已保存；在任意文件夹运行 traceh 可用。密钥不落盘。")
            elif action == "settings-save":
                path_text = self.query_one("#settings-profile-path", Input).value.strip()
                if not path_text:
                    raise ValueError("请填写配置文件路径。")
                save_profile(Path(path_text).expanduser(), values)
                self._status("非密钥启动配置已保存；临时密钥未保存。下次启动加载后生效。")
        except LaunchConfigurationError as error:
            self._status(f"操作失败：{error}")
        except Exception:
            # File/provider/parser exceptions can carry secrets. Never echo their
            # raw messages or invalid input. Field values remain in the form.
            self._status(
                "操作失败：请检查必填项、数值、路径和文件权限。"
                "工作区与会话 ID 二选一；真实模型需填写模型 ID 和 Base URL。"
                "现有运行环境未修改。"
            )

    @on(
        Input.Changed,
        "#setting-context_config, #setting-product_config, "
        "#setting-sandbox_config, #setting-background_config, #setting-plugins",
    )
    def _path_changed(self, event):
        kind = event.input.id.removeprefix("setting-").removesuffix("_config")
        self.query_one(f"#{kind}-enabled", Switch).value = bool(event.value.strip())
        if kind == "plugins":
            choices = self.query_one("#installed-plugins", SelectionList)
            desired = event.value.split()
            choices.deselect_all()
            for index in range(choices.option_count):
                option = choices.get_option_at_index(index)
                if option.value in desired:
                    choices.select(option.value)

    def _open_background_plan(self):
        from traceh.tui.optimization_plan import OptimizationPlanScreen

        value = self.query_one("#setting-background_config", Input).value.strip()
        path = (
            Path(value).resolve() if value
            else self._profile_path.parent / ".traceh-background.json"
        )
        workspace = self.query_one("#setting-workspace", Input).value.strip()
        data_dir = self.query_one("#setting-data_dir", Input).value.strip()
        model = {k: self.query_one(f"#setting-{k}", Input).value.strip()
                 for k in ("provider", "model", "base_url", "api_key_env")}

        def saved(result):
            if result is not None:
                self.query_one("#setting-background_config", Input).value = str(result)
                self.query_one("#background-enabled", Switch).value = True
                self._status("选题与周期额度已保存；应用配置后按 F6 开启，不会自动采用候选。")

        self.app.push_screen(OptimizationPlanScreen(
            config_path=path, workspace=workspace, data_dir=data_dir, model_settings=model,
        ), saved)

    def _open_form(self, kind):
        from traceh.tui.config_forms import (
            ConfigForm,
            background_preset,
            context_preset,
            product_preset,
            sandbox_preset,
            validate_document,
        )

        value = self.query_one(f"#setting-{kind}_config", Input).value.strip()
        path = (
            Path(value).expanduser().resolve()
            if value
            else (self._profile_path.resolve().parent / f".traceh-{kind}.json")
        )
        expected = None
        workspace = self.query_one("#setting-workspace", Input).value.strip()
        workspace = str(Path(workspace).expanduser().resolve()) if workspace else ""
        data_dir = self.query_one("#setting-data_dir", Input).value.strip()
        if path.exists():
            expected = path.read_bytes()
            raw = json.loads(expected.decode("utf-8"))
            validate_document(kind, raw, path)  # Validate before displaying any file content.
        else:
            raw = (
                context_preset()
                if kind == "context"
                else background_preset(workspace, data_dir)
                if kind == "background"
                else sandbox_preset()
                if kind == "sandbox"
                else product_preset(
                    workspace,
                    data_dir,
                    self.query_one("#setting-provider", Input).value,
                    self.query_one("#setting-model", Input).value,
                )
            )

        def saved(result):
            if result is not None:
                self.query_one(f"#setting-{kind}_config", Input).value = str(result)
                self.query_one(f"#{kind}-enabled", Switch).value = True
                self._status("功能配置已保存并选用。点击启动／应用配置生效；保存配置可记住该选择。")

        self.app.push_screen(
            ConfigForm(
                kind, path, raw, expected_bytes=expected, workspace=workspace, data_dir=data_dir
            ),
            saved,
        )

    def _context_action(self, action: str) -> None:
        value = self.query_one("#setting-context_config", Input).value.strip()
        if not value:
            raise ValueError("Context 路径必填")
        path = Path(value).expanduser().resolve()
        editor = self.query_one("#context-editor", TextArea)
        if action == "context-load":
            # Validate before showing any bytes: selecting a dotenv by accident
            # must not display its secrets in a non-password editor.
            body = path.read_bytes()
            raw = json.loads(body.decode("utf-8"))
            parse_context_host_config(raw, path=path)
            editor.load_text(json.dumps(raw, ensure_ascii=False, indent=2))
            self._loaded_context_path, self._loaded_context_bytes = path, body
            self._status("Context 已加载；编辑内容尚未保存。")
        else:
            raw = json.loads(editor.text)
            parse_context_host_config(raw, path=path)
            if path.exists():
                load_context_host_file(path)
                if (
                    path != self._loaded_context_path
                    or path.read_bytes() != self._loaded_context_bytes
                ):
                    self._status("目标已有内容或加载后已被修改。请先重新加载，或选择新的保存路径。")
                    return
            atomic_json(path, raw)
            self._loaded_context_path, self._loaded_context_bytes = path, path.read_bytes()
            self._status("Context 已保存；启动时由原有解析器加载。当前 Runtime 不会热切换。")

    def action_back(self) -> None:
        if self._startup or self._live_apply is not None:
            self.query_one("#settings-key", Input).value = ""
        self.dismiss(None)


class ConfigurationApp(App[argparse.Namespace | None]):
    TITLE = "TraceHarness · 启动配置"
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("f2", "settings", "配置", priority=True),
    ]

    def __init__(self, args, values, path, *, initial_error=""):
        super().__init__()
        self.theme = "textual-light"
        self._settings = SettingsScreen(
            args, values, path, startup=True, initial_error=initial_error
        )

    def on_mount(self) -> None:
        self.action_settings()

    def action_settings(self) -> None:
        if self.screen is self._settings:
            return
        self.push_screen(self._settings, self._settings_done)

    def _settings_done(self, args) -> None:
        self.exit(args)
