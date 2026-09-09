"""Textual presentation only; all governance belongs to the shared host service."""

import shlex
from uuid import uuid4

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Select, Static, TextArea

from traceh.chat.governance import display
from traceh.tui.text_selection import SelectableTextArea


class GovernanceScreen(Screen[bool | str]):
    BINDINGS = [
        Binding("escape", "back", "取消/返回", priority=True),
    ]
    CSS = """
    GovernanceScreen { layout: vertical; }
    #governance-title { height: auto; padding: 1 2; }
    #governance-evidence { height: 1fr; padding: 0 2; }
    #governance-confirm { dock: bottom; }
    #text-actions { height: 3; }
    """

    def __init__(self, value, *, confirmation=False):
        super().__init__()
        self._value = value
        self._confirmation = confirmation

    def compose(self) -> ComposeResult:
        yield Static(
            "宿主治理 · 确认前核对事实" if self._confirmation else "宿主治理 · 当前读取",
            id="governance-title",
        )
        yield SelectableTextArea(
            display(self._value),
            id="governance-evidence",
            read_only=True,
            soft_wrap=True,
            show_line_numbers=False,
        )
        with Horizontal(id="text-actions"):
            yield Button("返回 / 取消", id="close-evidence")
        if self._confirmation:
            yield Input(placeholder="输入 CONFIRM 执行；Esc 取消", id="governance-confirm")
        yield Footer()

    def on_mount(self):
        if self._confirmation:
            self.query_one("#governance-confirm", Input).focus()
        else:
            self.query_one("#governance-evidence", TextArea).focus()

    @on(Button.Pressed, "#close-evidence")
    def close_evidence(self):
        self.action_back()

    @on(Input.Submitted, "#governance-confirm")
    def confirmed(self, event):
        self.dismiss(event.value == "CONFIRM")

    def action_back(self):
        self.dismiss(False)


class MemoryScreen(GovernanceScreen):
    """Select real identities and return a command draft to the shared owner."""

    CSS = """
    #memory-form { height: auto; max-height: 70%; }
    #memory-buttons { height: 3; }
    #memory-error { height: auto; color: $error; }
    """

    def compose(self):
        yield Static("项目记忆 · 选择提议后填写槽位和署名，无需抄 ID", id="governance-title")
        yield SelectableTextArea(display(self._value), read_only=True, id="governance-evidence")
        with VerticalScroll(id="memory-form"):
            yield Select(
                [
                    (f"{p['body'][:60]} · {p['proposal_id']}", p["proposal_id"])
                    for p in self._value["proposals"]
                ],
                prompt="选择提议（按正文辨认）",
                id="memory-proposal",
            )
            yield Select(
                [
                    (f"{f['fact_slot']} · {f['body'][:60]}", f["memory_id"])
                    for f in self._value["active"]
                ],
                prompt="替换/撤销时选择现有事实",
                id="memory-active",
            )
            yield Input(placeholder="事实槽位（同一事实保持同名）", id="memory-slot")
            yield Input(placeholder="新记忆 ID（留空自动生成）", id="memory-id")
            yield Input(placeholder="操作人署名（必填）", id="memory-actor")
            yield Static("", id="memory-error", markup=False)
            with Horizontal(id="memory-buttons"):
                yield Button("审阅批准", id="memory-approve")
                yield Button("审阅替换", id="memory-supersede")
                yield Button("审阅撤销", id="memory-revoke")
        with Horizontal(id="text-actions"):
            yield Button("返回", id="close-evidence")
        yield Footer()

    @on(Select.Changed, "#memory-active")
    def active_selected(self, event):
        fact = next((f for f in self._value["active"] if f["memory_id"] == event.value), None)
        if fact is not None:
            self.query_one("#memory-slot", Input).value = fact["fact_slot"]

    @on(Button.Pressed, "#memory-approve, #memory-supersede, #memory-revoke")
    def draft_action(self, event):
        kind = event.button.id.removeprefix("memory-")
        actor = self.query_one("#memory-actor", Input).value.strip()
        proposal = self.query_one("#memory-proposal", Select).value
        active = self.query_one("#memory-active", Select).value
        slot = self.query_one("#memory-slot", Input).value.strip()
        error = ""
        if not actor:
            error = "请填写操作人署名。"
        elif kind == "revoke" and active is Select.BLANK:
            error = "请选择要撤销的现有事实。"
        elif kind != "revoke" and (proposal is Select.BLANK or not slot):
            error = "请选择提议并填写事实槽位。"
        elif kind == "supersede" and active is Select.BLANK:
            error = "请选择要替换的现有事实。"
        elif kind == "supersede":
            fact = next(f for f in self._value["active"] if f["memory_id"] == active)
            if fact["fact_slot"] != slot:
                error = "替换必须保留所选现有事实的槽位。"
        if error:
            self.query_one("#memory-error", Static).update(error)
            return
        if kind == "revoke":
            parts = ["/memory", kind, active, actor]
        else:
            identity = self.query_one("#memory-id", Input).value.strip() or str(uuid4())
            parts = ["/memory", kind, proposal, slot, identity, actor]
        self.dismiss(shlex.join(parts))
