"""The prompt: a multi-line editor that behaves like a chat box.

enter sends · shift+enter / alt+enter / ctrl+j insert a newline (terminals that
cannot tell shift+enter apart still get a newline key) · pasting an image path
attaches the image · long pastes are kept whole (TextArea has no size cap, unlike
Input — handoff trap #8)."""

from __future__ import annotations

import shlex
from pathlib import Path

from textual import events
from textual.message import Message
from textual.widgets import TextArea

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
NEWLINE_KEYS = {"shift+enter", "alt+enter", "ctrl+j", "newline", "ctrl+enter"}


def image_paths(text: str) -> list[str]:
    """Paths in a paste that point at existing image files (terminals paste a
    dragged file as its quoted/escaped path)."""
    text = text.strip()
    if not text or "\n" in text:
        return []
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = [text]
    found: list[str] = []
    for part in parts:
        path = Path(part).expanduser()
        if path.suffix.lower() in IMAGE_EXTS and path.is_file():
            found.append(str(path.resolve()))
    return found if len(found) == len(parts) else []


class Prompt(TextArea):
    class Submitted(Message):
        def __init__(self, prompt: Prompt, text: str, attachments: list[str]) -> None:
            super().__init__()
            self.prompt = prompt
            self.text = text
            self.attachments = attachments

        @property
        def control(self) -> Prompt:
            return self.prompt

    class AttachmentsChanged(Message):
        def __init__(self, prompt: Prompt) -> None:
            super().__init__()
            self.prompt = prompt

    def __init__(self, *, id: str | None = None, placeholder: str = "") -> None:
        super().__init__(
            "",
            id=id,
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            highlight_cursor_line=False,
            compact=True,
            placeholder=placeholder,
        )
        self.attachments: list[str] = []
        self.sent: list[str] = []
        self._hist_pos: int | None = None

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            # defer: messages posted from inside key dispatch can be dropped
            self.call_later(self.submit)
            return
        if event.key in NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "up" and self.cursor_location[0] == 0 and self.sent:
            event.stop()
            event.prevent_default()
            self._walk_history(-1)
            return
        if event.key == "down" and self._hist_pos is not None:
            last_row = self.document.line_count - 1
            if self.cursor_location[0] == last_row:
                event.stop()
                event.prevent_default()
                self._walk_history(1)
                return
        await super()._on_key(event)

    def _walk_history(self, delta: int) -> None:
        pos = len(self.sent) if self._hist_pos is None else self._hist_pos
        pos = max(0, min(len(self.sent), pos + delta))
        self._hist_pos = None if pos == len(self.sent) else pos
        self.text = "" if self._hist_pos is None else self.sent[pos]
        self.move_cursor(self.document.end)

    async def _on_paste(self, event: events.Paste) -> None:
        images = image_paths(event.text)
        if images:
            event.stop()
            event.prevent_default()
            self.attachments.extend(p for p in images if p not in self.attachments)
            self.post_message(self.AttachmentsChanged(self))
            return
        await super()._on_paste(event)

    def submit(self) -> None:
        text = self.text.strip()
        if not text and not self.attachments:
            return
        self.post_message(self.Submitted(self, text, list(self.attachments)))

    def accept(self) -> None:
        """The owner's view accepted the message: clear (and remember) it."""
        if self.text.strip():
            self.sent.append(self.text.strip())
        self._hist_pos = None
        self.text = ""
        self.attachments = []
        self.post_message(self.AttachmentsChanged(self))

    def clear_attachments(self) -> None:
        self.attachments = []
        self.post_message(self.AttachmentsChanged(self))
