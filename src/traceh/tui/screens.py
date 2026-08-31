"""Full-width read-only screens used by the single Textual application."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from traceh.product.chat import ProductStartRequest
from traceh.product.control import PendingProductProposal
from traceh.product.observation import ProductObservationReader
from traceh.tui.presentation import (
    ProductIdentityField,
    format_age,
    product_identity_fields,
    safe_display_block,
)
from traceh.tui.task_conversation import (
    TaskConversationReader,
    TaskConversationRole,
    TaskConversationSnapshot,
)


class TaskConversationScreen(Screen[None]):
    BINDINGS = [
        Binding("up", "previous_role", "上一角色", priority=True),
        Binding("down", "next_role", "下一角色", priority=True),
        Binding("enter", "toggle_role", "展开/折叠", priority=True),
        Binding("escape", "back", "返回", priority=True),
    ]
    CSS = """
    TaskConversationScreen { layout: vertical; }
    #task-conversation-title { height: 3; padding: 1 2; background: $panel; }
    #task-conversation-log { height: 1fr; padding: 1 3; }
    #task-conversation-status { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(
        self,
        reader: TaskConversationReader,
        observation_reader: ProductObservationReader | None,
        task_id: str | None,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._observation_reader = observation_reader
        self._task_id = task_id
        self._snapshot: TaskConversationSnapshot | None = None
        self._selected = 0
        self._expanded: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static("ProductTask 任务对话 · 打开时 fresh snapshot", id="task-conversation-title")
        yield RichLog(
            id="task-conversation-log",
            markup=False,
            highlight=False,
            wrap=True,
            max_lines=4_000,
        )
        yield Static("正在读取当前 durable facts…", id="task-conversation-status")
        yield Footer()

    async def on_mount(self) -> None:
        observation_reader = self._observation_reader
        task_id = self._task_id
        if observation_reader is None or task_id is None:
            self._show_unavailable("当前没有已建立的 durable ProductTask。")
            return
        try:
            observation = await observation_reader.load(task_id)
            if observation.summary is None:
                self._show_unavailable("当前没有已建立的 durable ProductTask。")
                return
            self._snapshot = await self._reader.load(observation)
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            self._show_unavailable(
                f"任务对话证据不可用 · {safe_display_block(code, limit=120, max_lines=1)}"
            )
            return
        default = self._snapshot.default_role_index
        if default is not None:
            self._selected = default
            self._expanded = {default}
        self._render_snapshot()

    def _show_unavailable(self, message: str) -> None:
        log = self.query_one("#task-conversation-log", RichLog)
        log.clear()
        log.write(safe_display_block(message))
        self.query_one("#task-conversation-status", Static).update(
            "这是只读失败；未写入、未猜测、未回退到其他 Session。"
        )

    def _render_snapshot(self) -> None:
        snapshot = self._snapshot
        log = self.query_one("#task-conversation-log", RichLog)
        log.clear()
        if snapshot is None:
            return
        if not snapshot.roles:
            log.write("本任务尚未建立 Router 或执行角色 Session。")
        for index, role in enumerate(snapshot.roles):
            selected = "›" if index == self._selected else " "
            expanded = "▾" if index in self._expanded else "▸"
            header = Text(
                f"{selected} {expanded} {role.role} · "
                f"{role.turns_started}/{role.turns_completed} turns · "
                f"{role.tool_calls} 工具调用 · {_usage_text(role)} · "
                f"{_age_text(role)}"
            )
            if index == self._selected:
                header.stylize("bold reverse")
            else:
                header.stylize("bold")
            log.write(header)
            if index in self._expanded:
                log.write(Text(f"    Session · {role.session_id}", style="dim"))
                visible, omitted = role.messages[:12], role.messages[12:]
                for kind, content in visible:
                    if kind == "tool":
                        first, result = content.split("\n", 1)
                        label, suffix = first.rsplit("\t", 1)
                        line = Text(f"    │ 工具 · {label}", style="blue")
                        line.append(
                            " " * max(1, self.size.width - line.cell_len - len(suffix) - 7)
                        )
                        line.append(suffix, style="dim")
                        log.write(line)
                        log.write(Text(f"    │      └ {result}", style="dim"))
                        continue
                    prefix = "    模型自述（非宿主证据） · " if kind == "model" else "    输入 · "
                    log.write(
                        Text.assemble(prefix, (content, "dim italic" if kind == "model" else ""))
                    )
                if omitted:
                    tools = sum(kind == "tool" for kind, _content in omitted)
                    speeches = len(omitted) - tools
                    log.write(
                        Text(f"    …还有 {tools} 次工具调用与 {speeches} 段发言", style="dim")
                    )
                if not role.messages:
                    log.write(Text("    尚无可见对话或工具活动。", style="dim"))
            log.write(Text("─" * 72, style="dim"))
        self.query_one("#task-conversation-status", Static).update(
            "打开时快照 · ↑/↓ 选择角色 · Enter 展开/折叠 · Esc 返回 · 不实时 tail"
        )

    async def action_previous_role(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.roles:
            return
        self._selected = (self._selected - 1) % len(snapshot.roles)
        self._render_snapshot()

    async def action_next_role(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.roles:
            return
        self._selected = (self._selected + 1) % len(snapshot.roles)
        self._render_snapshot()

    async def action_toggle_role(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.roles:
            return
        if self._selected in self._expanded:
            self._expanded.remove(self._selected)
        else:
            self._expanded.add(self._selected)
        self._render_snapshot()

    async def action_back(self) -> None:
        await self.dismiss()


class ProductIdentityScreen(Screen[None]):
    """Exact ProductTask identities with explicit, per-field copy keys."""

    BINDINGS = [
        Binding("c", "copy_task", "", show=False, priority=True),
        Binding("s", "copy_session", "", show=False, priority=True),
        Binding("r", "copy_review", "", show=False, priority=True),
        Binding("t", "copy_target", "", show=False, priority=True),
        Binding("p", "copy_patch", "", show=False, priority=True),
        Binding("d", "copy_digest", "", show=False, priority=True),
        Binding("escape", "back", "返回", priority=True),
    ]
    CSS = """
    ProductIdentityScreen { layout: vertical; }
    #identity-title { height: 3; padding: 1 2; background: $panel; }
    #identity-log { height: 1fr; padding: 1 3; }
    #identity-status { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(
        self,
        *,
        chat_session_id: str,
        proposal: PendingProductProposal | None,
        start_request: ProductStartRequest | None,
        observation_reader: ProductObservationReader | None,
        task_id: str | None,
    ) -> None:
        super().__init__()
        self._chat_session_id = chat_session_id
        self._proposal = proposal
        self._start_request = start_request
        self._observation_reader = observation_reader
        self._task_id = task_id
        self._fields: tuple[ProductIdentityField, ...] = ()
        self._fallback_path: Path | None = None

    @property
    def fallback_path(self) -> Path | None:
        return self._fallback_path

    @property
    def fields(self) -> tuple[ProductIdentityField, ...]:
        return self._fields

    def compose(self) -> ComposeResult:
        yield Static("ProductTask 完整身份", id="identity-title")
        yield RichLog(
            id="identity-log",
            markup=False,
            highlight=False,
            wrap=True,
            max_lines=500,
        )
        yield Static("Esc 返回", id="identity-status")
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#identity-log", RichLog)
        observation = None
        if self._task_id is not None:
            if self._observation_reader is None:
                self._show_unavailable("ProductTask observation reader 不可用。")
                return
            try:
                observation = await self._observation_reader.load(self._task_id)
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                self._show_unavailable(
                    "完整身份证据不可用 · "
                    f"{safe_display_block(code, limit=120, max_lines=1)}"
                )
                return
        self._fields = product_identity_fields(
            self._chat_session_id,
            self._proposal,
            self._start_request,
            observation,
        )
        if not self._fields:
            log.write("当前没有可显示的完整身份。")
            return
        for field in self._fields:
            line = Text(f"{field.label:<28} {safe_display_block(field.value)}")
            if field.copy_key is not None:
                line.append(f"    {field.copy_key} 复制", style="bold blue")
            log.write(line)

    def _show_unavailable(self, message: str) -> None:
        log = self.query_one("#identity-log", RichLog)
        log.clear()
        log.write(safe_display_block(message))
        self.query_one("#identity-status", Static).update(
            "这是只读失败；未使用旧快照、未猜测、未写入 durable facts。"
        )

    async def action_copy_task(self) -> None:
        self._copy("c")

    async def action_copy_session(self) -> None:
        self._copy("s")

    async def action_copy_review(self) -> None:
        self._copy("r")

    async def action_copy_target(self) -> None:
        self._copy("t")

    async def action_copy_patch(self) -> None:
        self._copy("p")

    async def action_copy_digest(self) -> None:
        self._copy("d")

    def _copy(self, key: str) -> None:
        field = next((item for item in self._fields if item.copy_key == key), None)
        status = self.query_one("#identity-status", Static)
        if field is None:
            status.update("当前身份尚未建立；没有内容可复制。")
            return
        try:
            self.app.copy_to_clipboard(field.value)
        except Exception:
            try:
                with NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="traceh-identity-",
                    suffix=".txt",
                    delete=False,
                ) as handle:
                    handle.write(field.value)
                    handle.write("\n")
                    self._fallback_path = Path(handle.name)
            except Exception:
                status.update("剪贴板和文件降级均不可用；身份仍完整显示在本页。")
                return
            status.update(
                f"剪贴板不可用；已写入 {safe_display_block(str(self._fallback_path))}"
            )
            return
        status.update(f"已复制 {field.label}。")

    async def action_back(self) -> None:
        await self.dismiss()


def _usage_text(role: TaskConversationRole) -> str:
    if role.usage_state == "not_started":
        return "尚无模型调用"
    if role.usage_tokens is None or role.usage_quality is None:
        return "用量不可用"
    prefix = "约 " if role.usage_quality == "estimated" else ""
    return f"{prefix}{role.usage_tokens} tok"


def _age_text(role: TaskConversationRole) -> str:
    age = role.last_fact_age_seconds
    return "最后事实不可用" if age is None else f"{format_age(age)}前"


__all__ = ["ProductIdentityScreen", "TaskConversationScreen"]
