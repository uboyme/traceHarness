"""Selectable human sandbox report, using the common read-only inspection owner."""

from textual import on
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Footer, Static

from traceh.tui.text_selection import SelectableTextArea


class SandboxScreen(Screen[None]):
    BINDINGS = [Binding("escape", "back", "返回", priority=True)]
    CSS = """
    SandboxScreen { layout: vertical; }
    #sandbox-title { height: auto; padding: 1 2; }
    #sandbox-report { height: 1fr; }
    """

    def __init__(self, report: str):
        super().__init__()
        self.report = report

    def compose(self):
        yield Static("沙箱 · 原始回执只读检查", id="sandbox-title", markup=False)
        yield SelectableTextArea(
            self.report, id="sandbox-report", read_only=True, soft_wrap=True,
            show_line_numbers=False,
        )
        yield Button("返回聊天", id="sandbox-close")
        yield Footer()

    @on(Button.Pressed, "#sandbox-close")
    def action_back(self):
        self.dismiss(None)
