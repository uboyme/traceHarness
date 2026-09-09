"""Small first-run and old-data choices; advanced configuration stays in Settings."""

from __future__ import annotations

from copy import copy

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Collapsible, Input, Label, Select, Static, Switch

from traceh.cli import credentials
from traceh.cli.tui_config import LaunchConfigurationError, form_values, preflight
from traceh.cli.tui_entry import save_personal_profile

# Explicit, user-selected service presets. No provider/model is selected implicitly.
# Official endpoint references are recorded in docs/validation-tui-simple-start.md.
SERVICES = {
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "bailian-cn": ("阿里云百炼 · 北京", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "bailian-intl": (
        "阿里云百炼 · 新加坡",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
    "deepseek": ("DeepSeek", "https://api.deepseek.com"),
}
MODEL_CHOICES = {
    "openai": ("gpt-4.1",),
    "bailian-cn": ("qwen-plus",),
    "bailian-intl": ("qwen-plus",),
    "deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
}


class QuickSetupApp(App):
    BINDINGS = [Binding("escape", "quit", "退出"), Binding("ctrl+q", "quit", "退出")]
    CSS = """
    Screen { align: center middle; }
    #setup { width: 76; max-width: 100%; height: 1fr; max-height: 100%; padding: 1 2; }
    #setup-actions { width: 76; max-width: 100%; height: 5; padding: 0 2; }
    Label, Static { height: auto; margin-bottom: 1; }
    Input, Select { margin-bottom: 1; }
    Button { margin: 1 1 0 0; }
    """

    def __init__(self, args, *, error=""):
        super().__init__()
        self.theme = "textual-light"
        self.args = args
        self.error = error

    def compose(self) -> ComposeResult:
        matched = next(
            (key for key, (_, url) in SERVICES.items() if url == self.args.base_url),
            "custom" if self.args.base_url else Select.NULL,
        )
        with VerticalScroll(id="setup"):
            yield Label("欢迎使用 TraceHarness · 模型配置一次，以后直接聊天")
            yield Static("工作目录和聊天记录位置自动管理。F2 随时修改完整配置。")
            yield Label("你的密钥属于哪个服务商？地区必须与密钥一致")
            yield Select(
                [(label, key) for key, (label, _) in SERVICES.items()]
                + [("其他兼容服务 / 本地模型", "custom")],
                value=matched,
                prompt="选择密钥所属服务商",
                id="setup-service",
            )
            yield Label("模型名称（使用该服务账号中已开通的模型）")
            yield Select(
                [(name, name) for name in MODEL_CHOICES.get(matched, ())]
                + [("填写其他模型名称", "custom")],
                prompt="选择模型，也可以填写下方名称",
                id="setup-model-choice",
            )
            yield Input(
                self.args.model or "", placeholder="填写服务商给出的模型名称", id="setup-model"
            )
            yield Label("API Key（已有环境配置时可以留空）")
            yield Input(password=True, id="setup-key")
            yield Label("记住密钥（Windows 当前用户加密保存，不写入启动配置）")
            yield Switch(
                credentials.available(), disabled=not credentials.available(), id="setup-remember"
            )
            with Collapsible(
                title="服务地址（选择服务商后自动填写；自定义服务请展开）",
                collapsed=matched != "custom",
                id="setup-endpoint",
            ):
                yield Input(self.args.base_url or "", id="setup-url")
            yield Static(self.error, id="setup-status", markup=False)
        with Horizontal(id="setup-actions"):
            yield Button("保存并开始聊天", id="setup-start", variant="primary")
            yield Button("完整配置", id="setup-advanced")

    @on(Select.Changed, "#setup-service")
    def service_changed(self, event):
        if event.value in SERVICES:
            self.query_one("#setup-url", Input).value = SERVICES[event.value][1]
        self.query_one("#setup-endpoint", Collapsible).collapsed = event.value != "custom"
        self.query_one("#setup-model-choice", Select).set_options(
            [(name, name) for name in MODEL_CHOICES.get(event.value, ())]
            + [("填写其他模型名称", "custom")]
        )

    @on(Select.Changed, "#setup-model-choice")
    def model_changed(self, event):
        if event.value is not Select.NULL and event.value != "custom":
            self.query_one("#setup-model", Input).value = event.value

    @on(Button.Pressed)
    def pressed(self, event):
        if event.button.id == "setup-advanced":
            self.exit("advanced")
            return
        if event.button.id != "setup-start":
            return
        try:
            args = copy(self.args)
            args.provider = "openai-compatible"
            args.model = self.query_one("#setup-model", Input).value.strip()
            args.base_url = self.query_one("#setup-url", Input).value.strip()
            key = self.query_one("#setup-key", Input).value
            args.tui_api_key = key or (
                getattr(self.args, "tui_api_key", None)
                if args.base_url == self.args.base_url
                else None
            )
            preflight(args)
            if key and self.query_one("#setup-remember", Switch).value:
                credentials.save_key(args, key)
            save_personal_profile(form_values(args))
            self.query_one("#setup-key", Input).value = ""
            self.exit(args)
        except LaunchConfigurationError as error:
            self.query_one("#setup-status", Static).update(str(error))
        except Exception:
            self.query_one("#setup-status", Static).update(
                "保存失败，请检查模型名称、服务地址或文件权限。已有聊天记录未修改。"
            )


class OldDataApp(App):
    BINDINGS = [Binding("escape", "quit", "退出")]
    CSS = QuickSetupApp.CSS

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="setup"):
            yield Label("发现旧版聊天记录")
            yield Static(
                "这些记录不能在当前版本继续。可以创建新的数据空间，"
                "旧记录原样保留；新的项目与记忆需要重新配置。"
            )
            yield Button("创建新版数据空间并开始", id="fresh-data", variant="primary")
            yield Button("返回配置", id="old-settings")
            yield Button("退出", id="old-exit")

    @on(Button.Pressed)
    def pressed(self, event):
        self.exit({"fresh-data": "fresh", "old-settings": "settings"}.get(event.button.id))
