"""Plain-language controls for the original background owner; no adoption button."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Static

from traceh.chat.background import (
    background_proposal_text,
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
            yield Label("后台检测与建议 · 只提建议，是否评测、是否采用都由你决定")
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
                yield Button("关闭这条建议（已处理或不采用）", id="optimization-dismiss")
                yield Button("刷新", id="optimization-refresh")
                yield Button("返回", id="optimization-back")
            yield Label("导入一次已结束的评测运行目录（必须是本周期绑定的题库）")
            yield Input(placeholder="评测输出目录", id="optimization-evaluation")
            yield Button("导入评测证据", id="optimization-import")
            yield Label("已核实被阻塞的建议调用：写明核实结论后解除（已预留额度不退）")
            yield Input(placeholder="例如：已在原证据确认调用已结束", id="optimization-ack-note")
            yield Button("核实后解除阻塞", id="optimization-acknowledge")
            yield Label(
                "续批或源码／设置变化后：填写新的到期时间（含时区），按当前设置批准一个新周期。"
            )
            yield Input(
                placeholder="到期时间，例如 2026-09-12T23:00:00+08:00", id="optimization-expiry"
            )
            yield Button("批准一个新周期", id="optimization-renew")
            yield Static(
                "开启后才检测。前台任务优先，任务结束后自动继续；退出应用会停止后台工作。",
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
        self.query_one("#optimization-acknowledge", Button).disabled = not (
            state["blocked"] and not state["active"]
        )
        if state["last_evidence"] and not state["active"]:
            reader = asyncio.create_task(
                asyncio.to_thread(background_proposal_text, state["last_evidence"])
            )
            try:
                detail = await asyncio.shield(reader)
            except asyncio.CancelledError:
                await await_worker_convergence(reader)
                raise
            except Exception:
                detail = "最近建议原件暂时无法核验，不能据缓存报告判断。"
            self.query_one("#optimization-experiment", Static).update(detail)

    @on(Button.Pressed)
    async def pressed(self, event):
        action = event.button.id
        if action == "optimization-back":
            self.dismiss()
            return
        try:
            notice = None
            if action == "optimization-submit":
                field = self.query_one("#optimization-feedback", Input)
                accepted = await submit_background_feedback(self.host, self.session_id, field.value)
                notice = (
                    "已记录问题线索；同类问题出现在至少两处来源后才会生成建议。"
                    if accepted
                    else "这轮反馈已记录，不重复执行。"
                )
                field.value = ""
            elif action == "optimization-import":
                field = self.query_one("#optimization-evaluation", Input)
                accepted = await self.host.observe_evaluation(Path(field.value.strip()))
                notice = "已导入评测中检测到的问题。" if accepted else "没有新的可记录问题。"
            elif action == "optimization-enable":
                await self.host.set_enabled(True)
            elif action == "optimization-pause":
                await self.host.set_enabled(False)
            elif action == "optimization-dismiss":
                await self.host.dismiss(self.pending)
            elif action == "optimization-acknowledge":
                field = self.query_one("#optimization-ack-note", Input)
                await self.host.acknowledge_blocked(field.value)
                field.value = ""
            elif action == "optimization-renew":
                expiry = datetime.fromisoformat(self.query_one("#optimization-expiry", Input).value)
                if expiry.tzinfo is None:
                    raise ValueError("explicit-timezone-required")
                await self.host.renew(expiry.astimezone(UTC))
            if notice is not None:
                self.query_one("#optimization-notice", Static).update(notice)
            await self.refresh_state()
        except Exception:
            self.query_one("#optimization-notice", Static).update(
                "操作未完成。请检查是否已开启、证据是否属于本题库、额度及未结算建议；原记录保留。"
            )

    def action_back(self):
        self.dismiss()
