"""Plain-language controls for the original background owner; no adoption button."""

import asyncio
from datetime import UTC, datetime

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Static

from traceh.chat.background import (
    background_experiment_text,
    background_status,
    submit_background_feedback,
)
from traceh.concurrency import await_worker_convergence


class OptimizationScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回", priority=True)]
    CSS = """
    OptimizationScreen { padding: 1 2; }
    #optimization-state { height: auto; margin-bottom: 1; }
    #optimization-notice { height: auto; color: $warning; }
    .optimization-actions { height: 3; }
    """

    def __init__(self, host, session_id):
        super().__init__()
        self.host, self.session_id = host, session_id
        self.pending = None

    def compose(self):
        with VerticalScroll():
            yield Label("后台受限优化 · 只提候选，由你决定是否采用")
            yield Static("正在读取原账本…", id="optimization-state", markup=False)
            yield Static("", id="optimization-experiment", markup=False)
            yield Label(
                "对最近一轮回复有什么问题？这里只发送你填写的反馈和证据定位，不发送完整聊天。"
            )
            yield Input(
                placeholder="例如：找到了目录，但没有读正文就回答了", id="optimization-feedback"
            )
            with Horizontal(classes="optimization-actions"):
                yield Button("提交反馈", id="optimization-submit")
                yield Button("开启／恢复", id="optimization-enable")
                yield Button("暂停并等待结束", id="optimization-pause")
            with Horizontal(classes="optimization-actions"):
                yield Button("不采用待审候选", id="optimization-dismiss")
                yield Button("刷新", id="optimization-refresh")
                yield Button("返回", id="optimization-back")
            yield Label("续批相同额度：填写新的到期时间（含时区）。不会自动开启，不会回放旧反馈。")
            yield Input(
                placeholder="到期时间，例如 2026-09-12T23:00:00+08:00", id="optimization-expiry"
            )
            yield Button("批准一个新的同额周期", id="optimization-renew")
            yield Static(
                "开启后才收集反馈。前台任务优先，退出应用会停止后台工作。",
                id="optimization-notice",
                markup=False,
            )

    async def on_mount(self):
        await self.refresh_state()

    async def refresh_state(self):
        state = await self.host.view()
        self.pending = state["pending_review"]
        self.query_one("#optimization-state", Static).update(background_status(state))
        self.query_one("#optimization-dismiss", Button).disabled = not bool(self.pending)
        if state["last_evidence"] and not state["active"]:
            reader = asyncio.create_task(
                asyncio.to_thread(background_experiment_text, state["last_evidence"])
            )
            try:
                detail = await asyncio.shield(reader)
            except asyncio.CancelledError:
                await await_worker_convergence(reader)
                raise
            except Exception:
                detail = "最近实验原件暂时无法核验，不能据缓存报告判断成功。"
            self.query_one("#optimization-experiment", Static).update(detail)

    @on(Button.Pressed)
    async def pressed(self, event):
        action = event.button.id
        if action == "optimization-back":
            self.dismiss()
            return
        try:
            if action == "optimization-submit":
                field = self.query_one("#optimization-feedback", Input)
                accepted = await submit_background_feedback(self.host, self.session_id, field.value)
                self.query_one("#optimization-notice", Static).update(
                    "已记录问题线索；符合额度与冷却条件后后台验证。"
                    if accepted
                    else "这轮反馈已记录，不重复执行。"
                )
                field.value = ""
            elif action == "optimization-enable":
                await self.host.set_enabled(True)
            elif action == "optimization-pause":
                await self.host.set_enabled(False)
            elif action == "optimization-dismiss":
                await self.host.dismiss(self.pending)
            elif action == "optimization-renew":
                expiry = datetime.fromisoformat(self.query_one("#optimization-expiry", Input).value)
                if expiry.tzinfo is None:
                    raise ValueError("explicit-timezone-required")
                await self.host.renew(expiry.astimezone(UTC))
            await self.refresh_state()
        except Exception:
            self.query_one("#optimization-notice", Static).update(
                "操作未完成。请检查是否已开启、是否有完成的对话、额度及未结算实验；原记录保留。"
            )

    def action_back(self):
        self.dismiss()
