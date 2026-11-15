"""Trajectory: the Claude-Code-style step log, used for mission feeds (Dashboard)
and conversations (Work). One renderer, so both look identical.

    › you typed this
    ⏺ orchestrator prose
    ⏺ read_file  path=src/app.py
      ⎿ 112 lines · def add(a, b): …
"""

from __future__ import annotations

import time
from typing import Any

from textual import events
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from rework.tui.theme import (
    ACCENT,
    DANGER,
    FAINT,
    FG,
    MUTED,
    OK,
    WARN,
    clip,
    esc,
    money,
    tokens,
)


def role_of(run_id: str | None) -> str:
    """'m-abc:code:coder:2' -> 'coder #2' (attempt shown only when > 1)."""
    if not run_id:
        return ""
    parts = run_id.split(":")
    if len(parts) >= 4:
        role, attempt = parts[2], parts[3]
        return role if attempt in ("1", "") else f"{role} #{attempt}"
    return run_id


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


class Step(Static):
    """One rendered step. Steps with a `detail` fold to one line; click toggles."""

    def __init__(self, markup: str, *, detail: str = "", kind: str = "note") -> None:
        super().__init__(markup, classes=f"step step-{kind}")
        self._head = markup
        self._detail = detail
        self._open = False
        if detail:
            self.add_class("-foldable")

    def on_click(self, event: events.Click) -> None:
        if not self._detail:
            return
        # clicks are for folding; a drag-select inside the step still copies
        self._open = not self._open
        self.set_class(self._open, "-open")
        self.update(f"{self._head}\n{self._detail}" if self._open else self._head)


def event_step(ev: Any, *, show_role: bool = True) -> Step | None:
    """Mission EventRecord -> Step (None = not worth a line)."""
    p: dict[str, Any] = ev.payload or {}
    kind = str(getattr(ev.type, "value", ev.type))
    who = role_of(ev.run_id)
    tag = f"[{FAINT}]{esc(who)}[/] " if (show_role and who) else ""
    traj = p.get("trajectory")
    actor = p.get("actor")
    if actor and show_role:
        tag = f"[{FAINT}]{esc(actor)}[/] "

    if traj == "turn":
        text = str(p.get("text") or "").strip()
        meta = f"turn {p.get('turn', '?')}"
        if p.get("tokens"):
            meta += f" · {tokens(p.get('tokens'))} tok"
        head = f"[{FG}]⏺[/] {tag}{esc(text) if text else f'[{MUTED}](thinking)[/]'}"
        return Step(f"{head}\n  [{FAINT}]{meta}[/]", kind="turn")
    if traj == "tool_call":
        name = str(p.get("name") or "tool")
        args = str(p.get("arguments") or "")
        return Step(
            f"[{ACCENT}]⏺[/] {tag}[b]{esc(name)}[/b]  [{MUTED}]{esc(clip(args, 160))}[/]",
            detail=f"  [{MUTED}]{esc(args)}[/]" if len(args) > 160 else "",
            kind="call",
        )
    if traj == "tool_result":
        error = str(p.get("error") or "")
        result = str(p.get("result") or "")
        body = error or result
        colour = DANGER if error else MUTED
        lines = body.strip().splitlines()
        first = clip(lines[0] if lines else "(empty)", 140)
        more = f" [{FAINT}]+{len(lines) - 1} lines[/]" if len(lines) > 1 else ""
        detail = ""
        if len(lines) > 1 or len(body) > 140:
            detail = "\n".join(f"    [{colour}]{esc(line)}[/]" for line in lines[:40])
        return Step(
            f"  [{FAINT}]⎿[/] [{colour}]{esc(first)}[/]{more}", detail=detail, kind="result"
        )

    if node := p.get("node"):
        if p.get("phase") == "enter":
            return Step(f"[{FAINT}]  → {tag}{esc(node)}[/]", kind="node")
        return None

    if kind == "mission_created":
        return Step(f"[{ACCENT}]▸[/] mission created [{FAINT}]· plan {esc(p.get('plan', ''))}[/]")
    if kind == "role_spawned":
        return Step(
            f"[{ACCENT}]▸[/] [b]{esc(who or p.get('role', 'role'))}[/b] started", kind="phase"
        )
    if kind == "role_completed":
        return Step(f"[{OK}]✓[/] [b]{esc(who or p.get('role', 'role'))}[/b] finished", kind="phase")
    if kind == "role_halted":
        q = str(p.get("question") or p.get("detail") or "")
        return Step(f"[{WARN}]◆[/] {tag}halted: {esc(clip(q, 200))}", kind="gate")
    if kind == "escalated":
        detail = str(p.get("detail") or p.get("question") or "")
        return Step(
            f"[{WARN}]◆[/] {tag}escalated [{MUTED}]{esc(p.get('kind', ''))}[/] "
            f"{esc(clip(detail, 200))}",
            kind="gate",
        )
    if kind == "model_promoted":
        return Step(f"[{ACCENT}]↑[/] {tag}model {esc(p.get('from'))} → [b]{esc(p.get('to'))}[/b]")
    if kind == "gate_opened":
        return Step(
            f"[{WARN}]◆ gate[/] [{MUTED}]{esc(p.get('kind', ''))}[/]  {esc(p.get('question', ''))}",
            kind="gate",
        )
    if kind == "gate_decided":
        ok = bool(p.get("approved"))
        mark = f"[{OK}]✓ approved[/]" if ok else f"[{DANGER}]✗ rejected[/]"
        note = p.get("note") or (p.get("decision") or {}).get("answer")
        extra = f" [{MUTED}]· {esc(clip(str(note), 160))}[/]" if note else ""
        return Step(f"{mark}{extra}", kind="gate")
    if kind == "mission_done":
        return Step(
            f"[{OK}]✓ mission done[/] [{FAINT}]· {p.get('runs', '?')} runs[/]", kind="phase"
        )
    if kind == "mission_failed":
        return Step(f"[{DANGER}]✗ mission failed[/]", kind="phase")
    if kind == "cancelled":
        return Step(f"[{FAINT}]⊘ cancelled {esc(p.get('reason', ''))}[/]", kind="phase")
    if kind == "plan_created":
        return Step(f"[{MUTED}]plan ready[/]")
    # progress notes / info
    if err := p.get("error"):
        return Step(f"[{DANGER}]![/] {tag}[{DANGER}]{esc(clip(str(err), 220))}[/]", kind="error")
    if note := p.get("note"):
        return Step(f"[{FAINT}]·[/] {tag}[{MUTED}]{esc(clip(str(note), 220))}[/]")
    if p:
        return Step(f"[{FAINT}]· {tag}{esc(kind)} {esc(clip(str(p), 160))}[/]")
    return None


def user_step(text: str, attachments: list[str] | None = None) -> Step:
    extra = ""
    if attachments:
        extra = "\n" + "\n".join(f"  [{FAINT}]⎘ {esc(a)}[/]" for a in attachments)
    return Step(f"[{MUTED}]›[/] {esc(text)}{extra}", kind="user")


def assistant_step(text: str) -> Step:
    return Step(f"[{FG}]⏺[/] {esc(text)}", kind="assistant")


def note_step(text: str, colour: str = MUTED) -> Step:
    return Step(f"[{colour}]{esc(text)}[/]", kind="note")


def cost_line(tok: int, usd: float) -> Step:
    return Step(f"  [{FAINT}]{tokens(tok)} tok · {money(usd)}[/]", kind="meta")


class Trajectory(VerticalScroll):
    """A scrolling step log that FOLLOWS the bottom unless the owner scrolled
    up to read (then it stays put until they scroll back down)."""

    MAX_STEPS = 1500

    def __init__(self, *, id: str | None = None, classes: str = "") -> None:
        super().__init__(id=id, classes=f"trajectory {classes}".strip())
        self.can_focus = True
        # the ONE trailing "working" line, updated in place by the spinner tick
        self._spinner: Step | None = None

    def _at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 2

    def add(self, *widgets: Widget) -> None:
        if not widgets:
            return
        follow = self._at_bottom()
        self.mount_all(list(widgets))
        overflow = len(self.children) - self.MAX_STEPS
        if overflow > 0:
            for child in list(self.children)[:overflow]:
                child.remove()
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def add_events(self, evs: list[Any], *, show_role: bool = True) -> None:
        steps = [s for s in (event_step(e, show_role=show_role) for e in evs) if s is not None]
        self.add(*steps)

    def spinner(self, markup: str | None) -> None:
        """Show/refresh/remove the trailing working line.

        `markup=None` removes it. The step is mounted once and then updated in
        place, so a 10fps tick costs one Static.update, not a remount (and the
        list does not flicker or jump)."""
        if markup is None:
            if self._spinner is not None:
                self._spinner.remove()
                self._spinner = None
            return
        if self._spinner is not None and self._spinner.parent is None:
            self._spinner = None  # trimmed as overflow by add()
        if self._spinner is None:
            step = Step(markup, kind="spinner")
            step.add_class("-spinner")
            self._spinner = step
            self.add(step)
            return
        if self._spinner._head != markup:
            self._spinner._head = markup
            self._spinner.update(markup)

    def clear(self) -> None:
        self.remove_children()
        self._spinner = None
