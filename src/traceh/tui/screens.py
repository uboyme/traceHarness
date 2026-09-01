"""Full-width read-only screens used by the single Textual application."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from traceh.artifacts.unified_diff import (
    PatchLineKind,
    UnifiedDiffFile,
    UnifiedDiffLine,
)
from traceh.product.chat import ProductStartRequest
from traceh.product.control import PendingProductProposal
from traceh.product.inspection import ProductPatchEvidence
from traceh.product.observation import ProductObservationReader
from traceh.tui.presentation import (
    MODEL_SELF_REPORT_COLOR,
    ProductIdentityField,
    format_age,
    prefixed_display_lines,
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
        yield Static("ProductTask 任务对话", id="task-conversation-title")
        yield RichLog(
            id="task-conversation-log",
            markup=False,
            highlight=False,
            wrap=True,
            auto_scroll=False,
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.call_after_refresh(self._render_title)
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
        self.call_after_refresh(self._render_snapshot)

    async def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._render_title)
        if self._snapshot is not None:
            self.call_after_refresh(self._render_snapshot)

    def _show_unavailable(self, message: str) -> None:
        log = self.query_one("#task-conversation-log", RichLog)
        log.clear()
        log.write(safe_display_block(message))
        log.write(Text("返回后重试。", style="dim"))

    def _render_snapshot(self) -> None:
        snapshot = self._snapshot
        log = self.query_one("#task-conversation-log", RichLog)
        log.clear()
        if snapshot is None:
            return
        if not snapshot.roles:
            log.write("本任务尚未建立 Router 或执行角色 Session。")
        header_rows: dict[int, int] = {}
        for index, role in enumerate(snapshot.roles):
            width = max(
                1,
                log.content_region.width - log.styles.scrollbar_size_vertical,
            )
            header_rows[index] = len(log.lines)
            headers = _role_header_lines(
                role,
                expanded=index in self._expanded,
                selected=index == self._selected,
                width=width,
            )
            for header in headers:
                log.write(header, width=max(1, header.cell_len))
            if index in self._expanded:
                log.write(
                    Text(f"  {_short_identifier(role.session_id)}", style="dim")
                )
                for kind, content in role.messages:
                    if kind == "tool":
                        first, result = content.split("\n", 1)
                        label, suffix = first.rsplit("\t", 1)
                        marker = "  ▏ "
                        label_prefix = "工具 · "
                        wrapped = prefixed_display_lines(
                            label,
                            width=width,
                            first_prefix=marker + label_prefix,
                            continuation_prefix=(
                                marker + " " * Text(label_prefix).cell_len
                            ),
                        )
                        for line_index, (prefix, segment) in enumerate(wrapped):
                            line = Text()
                            line.append(marker, style="blue")
                            line.append(prefix[len(marker) :] + segment)
                            suffix_width = Text(suffix).cell_len
                            if (
                                line_index == 0
                                and len(wrapped) == 1
                                and line.cell_len + 1 + suffix_width <= width
                            ):
                                line.append(" " * (width - line.cell_len - suffix_width))
                                line.append(suffix, style="dim")
                            log.write(line, width=max(1, line.cell_len))
                        result_prefix = "     └ "
                        for prefix, segment in prefixed_display_lines(
                            result,
                            width=width,
                            first_prefix=marker + result_prefix,
                            continuation_prefix=(
                                marker + " " * Text(result_prefix).cell_len
                            ),
                        ):
                            result_line = Text()
                            result_line.append(marker, style="blue")
                            result_line.append(prefix[len(marker) :], style="dim")
                            result_line.append(
                                segment,
                                style=(
                                    "yellow"
                                    if result.startswith("完成 · exit=")
                                    else "dim"
                                ),
                            )
                            log.write(
                                result_line,
                                width=max(1, result_line.cell_len),
                            )
                        continue
                    if kind == "model":
                        marker = "  ▏ "
                        label = "模型 · "
                        lines = prefixed_display_lines(
                            _collapse_model_blank_lines(content),
                            width=width,
                            first_prefix=marker + label,
                            continuation_prefix=(
                                marker + " " * Text(label).cell_len
                            ),
                        )
                        for prefix, segment in lines:
                            line = Text()
                            line.append(prefix[: len(marker)], style="dim")
                            line.append(
                                prefix[len(marker) :] + segment,
                                style=f"italic {MODEL_SELF_REPORT_COLOR}",
                            )
                            log.write(line, width=max(1, line.cell_len))
                        continue
                    for prefix, segment in prefixed_display_lines(
                        content,
                        width=width,
                        first_prefix="    输入 · ",
                    ):
                        line = Text(prefix + segment)
                        log.write(line, width=max(1, line.cell_len))
                if not role.messages:
                    log.write(Text("  尚无可见对话或工具活动。", style="dim"))
        target = header_rows.get(self._selected, 0)
        self.call_after_refresh(
            lambda: log.scroll_to(y=target, animate=False)
        )

    def _render_title(self) -> None:
        title = self.query_one("#task-conversation-title", Static)
        left = "ProductTask 任务对话"
        right = "打开时快照 · 不实时 tail"
        width = max(1, title.content_region.width)
        gap = width - Text(left).cell_len - Text(right).cell_len
        if gap < 1:
            right = "快照 · 非实时"
            gap = width - Text(left).cell_len - Text(right).cell_len
        rendered = Text(left)
        if gap >= 1:
            rendered.append(" " * gap)
            rendered.append(right, style="dim")
        title.update(rendered)

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


class ProductChangesScreen(Screen[None]):
    """Fresh, read-only view of one exact CAS-backed patch artifact."""

    BINDINGS = [
        Binding("up", "previous_file", "上一文件", priority=True),
        Binding("down", "next_file", "下一文件", priority=True),
        Binding("enter", "toggle_file", "展开/折叠", priority=True),
        Binding("ctrl+e", "export_patch", "导出", priority=True),
        Binding("escape", "back", "返回", priority=True),
    ]
    CSS = """
    ProductChangesScreen { layout: vertical; }
    #changes-title { height: 3; padding: 1 2; background: $panel; }
    #changes-log { height: 1fr; padding: 1 3; }
    #changes-status { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(
        self,
        *,
        observation_reader: ProductObservationReader | None,
        task_id: str | None,
    ) -> None:
        super().__init__()
        self._observation_reader = observation_reader
        self._task_id = task_id
        self._patch: ProductPatchEvidence | None = None
        self._selected = 0
        self._expanded: set[int] = set()
        self._export_path: Path | None = None

    @property
    def export_path(self) -> Path | None:
        return self._export_path

    def compose(self) -> ComposeResult:
        yield Static("ProductTask 完整改动 · 打开时快照", id="changes-title")
        yield RichLog(
            id="changes-log",
            markup=False,
            highlight=False,
            wrap=True,
            auto_scroll=False,
            max_lines=None,
        )
        yield Static("正在读取完整补丁证据…", id="changes-status")
        yield Footer()

    async def on_mount(self) -> None:
        reader = self._observation_reader
        task_id = self._task_id
        if reader is None or task_id is None:
            self._show_unavailable("当前没有已建立的 durable ProductTask。")
            return
        try:
            patch = await reader.load_patch(task_id)
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            self._show_unavailable(
                "完整改动证据不可用 · "
                f"{safe_display_block(code, limit=120, max_lines=1)}"
            )
            return
        if patch is None:
            self._show_unavailable("当前任务尚未建立补丁证据。")
            return
        self._patch = patch
        if patch.diff.files:
            self._expanded = {0}
        self.call_after_refresh(self._render_patch)

    async def on_resize(self, event: Resize) -> None:
        del event
        if self._patch is not None:
            self.call_after_refresh(self._render_patch)

    def _show_unavailable(self, message: str) -> None:
        log = self.query_one("#changes-log", RichLog)
        log.clear()
        log.write(safe_display_block(message))
        self.query_one("#changes-status", Static).update(
            "无法读取完整改动；请返回后重试。"
        )

    def _render_patch(self) -> None:
        patch = self._patch
        log = self.query_one("#changes-log", RichLog)
        log.clear()
        if patch is None:
            return
        files = patch.diff.files
        if not files:
            log.write("完整补丁无法按文件显示；仍可按 Ctrl+E 导出。")
        header_rows: dict[int, int] = {}
        for index, file in enumerate(files):
            header_rows[index] = len(log.lines)
            header = self._file_header(file, index, log)
            log.write(header, width=max(1, header.cell_len))
            if index in self._expanded:
                if file.summary.binary and not file.lines:
                    log.write(Text("    二进制改动", style="dim"))
                for line in file.lines:
                    width = max(
                        1,
                        log.content_region.width
                        - log.styles.scrollbar_size_vertical,
                    )
                    for rendered in _patch_line_texts(line, width=width):
                        log.write(rendered, width=max(1, rendered.cell_len))
        summary = patch.diff.summary
        counts = (
            "统计不可用"
            if summary.additions is None or summary.deletions is None
            else f"+{summary.additions} −{summary.deletions}"
        )
        self.query_one("#changes-status", Static).update(
            f"{len(summary.files)} 文件 · {patch.patch_size_bytes} bytes · {counts}"
        )
        target = header_rows.get(self._selected, 0)
        self.call_after_refresh(
            lambda: log.scroll_to(y=target, animate=False)
        )

    def _file_header(
        self,
        file: UnifiedDiffFile,
        index: int,
        log: RichLog,
    ) -> Text:
        expanded = "▾" if index in self._expanded else "▸"
        summary = file.summary
        path = _safe_full_line(summary.path)
        status = _patch_status_text(summary.status)
        counts = (
            "二进制"
            if summary.binary
            else _patch_counts(summary.additions, summary.deletions)
        )
        label = f"{expanded} {path} · {status} · {counts} "
        width = max(
            1,
            log.content_region.width - log.styles.scrollbar_size_vertical,
        )
        line = Text(label)
        if line.cell_len < width:
            line.append("─" * (width - line.cell_len))
        line.stylize("bold blue" if index == self._selected else "dim")
        return line

    async def action_previous_file(self) -> None:
        patch = self._patch
        if patch is None or not patch.diff.files:
            return
        self._selected = (self._selected - 1) % len(patch.diff.files)
        self._render_patch()

    async def action_next_file(self) -> None:
        patch = self._patch
        if patch is None or not patch.diff.files:
            return
        self._selected = (self._selected + 1) % len(patch.diff.files)
        self._render_patch()

    async def action_toggle_file(self) -> None:
        patch = self._patch
        if patch is None or not patch.diff.files:
            return
        if self._selected in self._expanded:
            self._expanded.remove(self._selected)
        else:
            self._expanded.add(self._selected)
        self._render_patch()

    async def action_export_patch(self) -> None:
        patch = self._patch
        status = self.query_one("#changes-status", Static)
        if patch is None:
            status.update("当前没有可导出的完整补丁。")
            return
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix="traceh-patch-",
                suffix=".patch",
                delete=False,
            ) as handle:
                handle.write(patch.content)
                self._export_path = Path(handle.name)
        except Exception:
            status.update("完整补丁导出失败。")
            return
        status.update(_safe_full_line(str(self._export_path)))

    async def action_back(self) -> None:
        await self.dismiss()


def _usage_text(role: TaskConversationRole) -> str:
    if role.usage_state == "not_started":
        return "尚无模型调用"
    if role.usage_tokens is None or role.usage_quality is None:
        return "用量不可用"
    prefix = "约 " if role.usage_quality == "estimated" else ""
    return f"{prefix}{role.usage_tokens} tok"


def _role_header_lines(
    role: TaskConversationRole,
    *,
    expanded: bool,
    selected: bool,
    width: int,
) -> tuple[Text, ...]:
    left = f"{'▾' if expanded else '▸'} {_safe_full_line(role.role)} "
    left_width = Text(left).cell_len
    facts = " · ".join(
        (
            f"{role.turns_started}/{role.turns_completed} turns",
            f"{role.tool_calls} 工具",
            _usage_text(role),
            _age_text(role),
        )
    )
    right = " " + facts
    style = "bold blue" if selected else "dim"
    if left_width + Text(right).cell_len + 1 <= width:
        separator = "─" * (width - left_width - Text(right).cell_len)
        header = Text(left + separator + right, style=style)
        return (header,)

    role_rows = prefixed_display_lines(
        _safe_full_line(role.role),
        width=width,
        first_prefix=f"{'▾' if expanded else '▸'} ",
        continuation_prefix="  ",
    )
    rendered: list[Text] = []
    for prefix, segment in role_rows:
        row = Text(prefix + segment, style=style)
        if row.cell_len < width:
            row.append("─" * (width - row.cell_len), style=style)
        rendered.append(row)
    for prefix, segment in prefixed_display_lines(
        facts,
        width=width,
        first_prefix="  ",
        continuation_prefix="  ",
    ):
        rendered.append(Text(prefix + segment, style=style))
    return tuple(rendered)


def _short_identifier(value: str, *, max_chars: int = 20) -> str:
    rendered = _safe_full_line(value)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 1] + "…"


def _collapse_model_blank_lines(content: str) -> str:
    rendered: list[str] = []
    previous_blank = False
    for line in content.split("\n"):
        blank = not line.strip()
        if blank and previous_blank:
            continue
        rendered.append("" if blank else line)
        previous_blank = blank
    return "\n".join(rendered)


def _age_text(role: TaskConversationRole) -> str:
    age = role.last_fact_age_seconds
    return "最后事实不可用" if age is None else f"{format_age(age)}前"


def _safe_full_line(value: str) -> str:
    limit = max(1, len(value) * 8 + 1)
    return safe_display_block(
        value,
        limit=limit,
        max_lines=1,
        line_limit=limit,
    )


def _patch_status_text(status: str) -> str:
    return {
        "added": "新增",
        "modified": "修改",
        "deleted": "删除",
        "renamed": "重命名",
    }.get(status, "状态未知")


def _patch_counts(additions: int | None, deletions: int | None) -> str:
    added = "?" if additions is None else str(additions)
    deleted = "?" if deletions is None else str(deletions)
    return f"+{added} −{deleted}"


def _patch_line_texts(
    line: UnifiedDiffLine,
    *,
    width: int,
) -> tuple[Text, ...]:
    number = line.new_line
    marker = " "
    style = "dim"
    if line.kind is PatchLineKind.ADDITION:
        marker = "+"
        style = "green"
        number = line.new_line
    elif line.kind is PatchLineKind.DELETION:
        marker = "-"
        style = "red"
        number = line.old_line
    number_text = "     " if number is None else f"{number:>5}"
    first_prefix = f"{number_text} {marker} "
    continuation = " " * Text(first_prefix).cell_len
    rendered: list[Text] = []
    for index, (prefix, segment) in enumerate(
        prefixed_display_lines(
            _safe_full_line(line.text),
            width=width,
            first_prefix=first_prefix,
            continuation_prefix=continuation,
        )
    ):
        physical = Text()
        if index == 0:
            physical.append(prefix[:5], style="dim")
            physical.append(prefix[5:], style=style)
        else:
            physical.append(prefix, style="dim")
        physical.append(segment, style=style)
        rendered.append(physical)
    return tuple(rendered)


__all__ = [
    "ProductChangesScreen",
    "ProductIdentityScreen",
    "TaskConversationScreen",
]
