"""Human choices over current Session and project readers, with no independent state."""

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from traceh.session.protocol import SessionProtocolError


async def session_choices(runtime, workspace):
    from traceh.agents.directory import AgentDirectoryReader

    directory = await AgentDirectoryReader(runtime.sessions.store).load()
    rows = []
    for sid in await runtime.sessions.list_sessions():
        if directory.for_session(sid) is not None:
            continue
        try:
            events = await runtime.sessions.read_session(sid)
        except SessionProtocolError:
            continue
        if not events or await runtime.sessions.workspace_for(sid) != workspace:
            continue
        first = next(
            (e.data.get("content", "") for e in events if e.type == "user/message"), "空对话"
        )
        title = " ".join(str(first).split())[:60]
        when = events[-1].occurred_at.isoformat(timespec="seconds")
        rows.append((when, f"{when} · {title} · {sid[:8]}", sid))
    return tuple((label, sid) for _, label, sid in sorted(rows, reverse=True))


class SessionPicker(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self, choices):
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("此文件夹的对话 · 选择继续，或开始新对话")
            yield Select(self.choices, prompt="选择一段对话", id="session-choice")
            yield Button("继续选中的对话", id="session-resume")
            yield Button("新对话", id="session-new", variant="primary")
            yield Button("返回聊天", id="session-back")

    @on(Button.Pressed)
    def pressed(self, event):
        if event.button.id == "session-new":
            self.dismiss("")
        elif event.button.id == "session-back":
            self.dismiss(None)
        elif event.button.id == "session-resume":
            value = self.query_one("#session-choice", Select).value
            if value is not Select.NULL:
                self.dismiss(value)

    def action_back(self):
        self.dismiss(None)


class ProjectChoiceApp(App):
    BINDINGS = [Binding("escape", "quit", "暂不关联")]

    def __init__(self, choices):
        super().__init__()
        self.choices = choices
        self.theme = "textual-light"

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("为这个文件夹选择项目")
            yield Static(
                "关联后可使用该项目已批准的记忆。这里只显示已核对来源的项目；不会批准新记忆。"
            )
            yield Select(
                [(label, pid) for pid, label in self.choices],
                id="project-choice",
                value=self.choices[0][0] if len(self.choices) == 1 else Select.NULL,
            )
            yield Label("你的操作署名（首次填写，用于关联记录）")
            yield Input(id="project-actor")
            yield Label("以后这个文件夹的新对话自动关联所选项目")
            yield Switch(True, id="project-remember")
            yield Static("", id="project-choice-status")
            yield Button("确认关联", id="project-bind", variant="primary")
            yield Button("暂不关联", id="project-skip")

    @on(Button.Pressed)
    def pressed(self, event):
        if event.button.id == "project-skip":
            self.exit(None)
        elif event.button.id == "project-bind":
            from traceh.projects.events import identifier

            project = self.query_one("#project-choice", Select).value
            actor = self.query_one("#project-actor", Input).value.strip()
            try:
                identifier(actor)
                if project is Select.NULL:
                    raise ValueError
            except ValueError:
                self.query_one("#project-choice-status", Static).update(
                    "请选择项目并填写合法署名。"
                )
                return
            self.exit((project, actor, self.query_one("#project-remember", Switch).value))
