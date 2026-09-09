"""Direct text selection and a small context menu; presentation data only."""

from rich.cells import cell_len
from rich.segment import Segment
from textual import on
from textual.binding import Binding
from textual.events import MouseDown
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Button, RichLog, TextArea


class CopyRequested(Message):
    def __init__(self, text: str, position: Offset):
        super().__init__()
        self.text = text
        self.position = position


class SelectableLog(RichLog):
    """Attach original character offsets to RichLog's already rendered lines."""

    def get_selection(self, selection: Selection):
        return selection.extract("\n".join(line.text for line in self.lines)), "\n"

    def render_line(self, y: int) -> Strip:
        row = y + self.scroll_offset.y
        if row >= len(self.lines):
            return super().render_line(y)
        line = self.lines[row].apply_style(self.rich_style)
        text = line.text
        selection = self.text_selection
        span = selection.get_span(row) if selection else None
        if span is not None:
            start, end = span
            end = len(text) if end == -1 else end
            left, right = cell_len(text[:start]), cell_len(text[:end])
            if left < right:
                line = Strip.join(
                    (
                        line.crop(0, left),
                        Strip(Segment.apply_style(
                            line.crop(left, right), post_style=self.selection_style,
                        )),
                        line.crop(right, line.cell_length),
                    )
                )
        scroll_x = self.scroll_offset.x
        character = 0
        while character < len(text) and cell_len(text[:character]) < scroll_x:
            character += 1
        return (
            line.crop_extend(
                scroll_x,
                scroll_x + self.scrollable_content_region.width,
                self.rich_style,
            )
            .apply_offsets(character, row)
        )

    def on_mouse_down(self, event: MouseDown):
        if event.button == 3:
            event.stop()
            event.prevent_default()
            selection = self.text_selection
            text = self.get_selection(selection)[0] if selection else ""
            self.post_message(CopyRequested(text, event.screen_offset))


class SelectableTextArea(TextArea):
    async def _on_mouse_down(self, event: MouseDown):
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self.post_message(CopyRequested(self.selected_text, event.screen_offset))
        else:
            await super()._on_mouse_down(event)


class CopyMenu(ModalScreen[None]):
    BINDINGS = [Binding("escape", "back", show=False)]
    CSS = """
    CopyMenu { background: transparent; }
    #selection-copy { position: absolute; width: 12; min-width: 12; height: 3; }
    """

    def __init__(self, text: str, position: Offset):
        super().__init__()
        self._text = text
        self._position = position

    def compose(self):
        yield Button("复制", id="selection-copy", disabled=not self._text)

    def on_mount(self):
        button = self.query_one(Button)
        button.styles.offset = Offset(
            max(0, min(self._position.x, self.size.width - 12)),
            max(0, min(self._position.y, self.size.height - 3)),
        )
        button.focus()

    @on(Button.Pressed, "#selection-copy")
    def copy_selection(self):
        if not self._text:
            return
        self.app.copy_to_clipboard(self._text)
        self.dismiss()

    def on_mouse_down(self, event: MouseDown):
        if event.button == 1 and event.widget is self:
            self.dismiss()

    def action_back(self):
        self.dismiss()
