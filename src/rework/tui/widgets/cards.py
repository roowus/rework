"""Inline cards in the conversation: a proposal awaiting consent, and a live
mission (status, last steps, its gate with answer buttons, controls)."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from rework.tui.actions import can
from rework.tui.theme import (
    ACCENT,
    FAINT,
    MUTED,
    WARN,
    ago,
    clip,
    esc,
    money,
    plan_label,
    status_text,
    status_value,
)
from rework.tui.widgets.base import Btn
from rework.tui.widgets.trajectory import event_step, role_of


class ProposalCard(Vertical):
    """The orchestrator's proposal. Nothing exists until Launch."""

    class Decided(Message):
        def __init__(self, card: ProposalCard, launch: bool) -> None:
            super().__init__()
            self.card = card
            self.launch = launch

    def __init__(self, proposal: Any, *, live: bool = True) -> None:
        super().__init__(classes="card -accent proposal")
        self.proposal = proposal
        self.live = live

    def compose(self) -> ComposeResult:
        p = self.proposal
        meta = [f"plan [b]{esc(p.plan or 'fix')}[/b]"]
        if p.project:
            meta.append(f"project [b]{esc(p.project)}[/b]")
        if p.repo:
            meta.append(f"repo {esc(clip(p.repo, 60))}")
        if p.model:
            meta.append(f"model {esc(p.model)}")
        if p.effort:
            meta.append(f"effort {esc(p.effort)}")
        title = getattr(p, "title", "") or ""
        heading = (
            f"[b]{esc(title)}[/b]\n{esc(p.objective)}" if title else f"[b]{esc(p.objective)}[/b]"
        )
        yield Static(
            f"[{ACCENT}]PROPOSAL[/]\n{heading}\n[{MUTED}]" + "  ·  ".join(meta) + "[/]",
            classes="card-body",
        )
        with Horizontal(classes="card-actions"):
            yield Btn("Launch", "launch", variant="primary")
            yield Btn("Scrap", "scrap", variant="ghost")
            yield Static(f"[{FAINT}]or reply with changes · “yes” launches[/]", classes="spacer")

    def on_mount(self) -> None:
        self.set_live(self.live)

    def set_live(self, live: bool) -> None:
        self.live = live
        self.query_one(".card-actions").display = live
        if not live:
            self.remove_class("-accent")

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        event.stop()
        if event.action in ("launch", "scrap"):
            self.post_message(self.Decided(self, event.action == "launch"))


class MissionCard(Vertical):
    """A mission launched from this chat, kept live by the Work view."""

    class Act(Message):
        def __init__(self, card: MissionCard, verb: str, gate_id: str = "") -> None:
            super().__init__()
            self.card = card
            self.verb = verb
            self.gate_id = gate_id

    TAIL = 5

    def __init__(self, mission_id: str) -> None:
        super().__init__(classes="card mission-card")
        self.mission_id = mission_id
        self.mission: Any = None
        self.gate: Any = None
        self.spend = 0.0
        self.last_seq = 0
        self.tail: list[str] = []
        self.active_role = ""

    def compose(self) -> ComposeResult:
        yield Static(f"[{MUTED}]mission {esc(self.mission_id)}[/]", classes="card-body mc-head")
        yield Static("", classes="card-body mc-tail")
        yield Static("", classes="card-body mc-gate")
        with Horizontal(classes="card-actions mc-actions"):
            yield Btn("Open", "open")
            yield Btn("Approve", "approve", variant="primary", classes="mc-gatebtn")
            yield Btn("Reject", "reject", variant="danger", classes="mc-gatebtn")
            yield Btn("Answer…", "answer", variant="ghost", classes="mc-gatebtn")
            yield Btn("Pause", "pause", variant="ghost")
            yield Btn("Resume", "resume", variant="ghost")
            yield Btn("Cancel", "cancel", variant="ghost")

    def on_mount(self) -> None:
        self.paint()

    @property
    def status(self) -> str:
        return status_value(self.mission.status) if self.mission is not None else ""

    def update_from(self, mission: Any, gates: list[Any]) -> None:
        self.mission = mission
        self.gate = gates[0] if gates else None
        self.paint()

    def add_events(self, events: list[Any]) -> None:
        for ev in events:
            self.last_seq = max(self.last_seq, int(ev.seq or 0))
            if ev.run_id:
                self.active_role = role_of(ev.run_id)
            step = event_step(ev)
            if step is None:
                continue
            markup = str(step._head).split("\n")[0]
            self.tail.append(markup)
        self.tail = self.tail[-self.TAIL :]
        self.paint()

    def paint(self) -> None:
        # children may not be composed yet (or already gone during teardown)
        if not self.is_mounted or not self.query(".mc-head"):
            return
        m = self.mission
        head = self.query_one(".mc-head", Static)
        if m is None:
            head.update(f"[{MUTED}]mission {esc(self.mission_id)} · loading…[/]")
        else:
            status = status_value(m.status)
            bits = [esc(self.mission_id), esc(plan_label(m.plan))]
            if self.active_role and status in {"running", "awaiting_gate"}:
                bits.append(f"[b]{esc(self.active_role)}[/b]")
            if self.spend:
                bits.append(money(self.spend))
            bits.append(ago(m.created_at))
            name = (
                f"[b]{esc(m.title)}[/b]  [{MUTED}]{esc(clip(m.objective, 140))}[/]"
                if getattr(m, "title", None)
                else f"[b]{esc(clip(m.objective, 200))}[/b]"
            )
            head.update(f"{status_text(status)}  [{MUTED}]" + "  ·  ".join(bits) + f"[/]\n{name}")
            for cls in ("-accent", "-warn", "-ok", "-danger"):
                self.remove_class(cls)
            self.add_class(
                {
                    "running": "-accent",
                    "awaiting_gate": "-warn",
                    "on_hold": "-warn",
                    "done": "-ok",
                    "failed": "-danger",
                }.get(status, "-none")
            )
        tail = self.query_one(".mc-tail", Static)
        tail.display = bool(self.tail)
        tail.update("\n".join(f"  {line}" for line in self.tail))
        gate = self.query_one(".mc-gate", Static)
        gate.display = self.gate is not None
        if self.gate is not None:
            gate.update(f"[{WARN}]◆ needs you[/]  {esc(self.gate.question)}")
        yes_no = self.gate is not None and self.gate.kind.value in (
            "ship",
            "open_pr",
            "generic",
            "spawn",
            "permission",
        )
        for btn in self.query(".mc-gatebtn").results(Btn):
            if btn.action == "answer":
                btn.display = self.gate is not None
            else:
                btn.display = yes_no  # questions and choices need the Gates tab
        status = self.status
        for verb in ("pause", "resume", "cancel"):
            for btn in self.query(Btn):
                if btn.action == verb:
                    btn.display = can(verb, status)

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        event.stop()
        gate_id = self.gate.id if self.gate is not None else ""
        self.post_message(self.Act(self, event.action, gate_id))
