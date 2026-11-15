"""Inline cards in the conversation: a proposal awaiting consent, and a live
mission (status, last steps, its gate with answer buttons, controls)."""

from __future__ import annotations

import math
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, Static

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


def _tail_line(ev: Any) -> str | None:
    """One tail line for an event, or None when it does not belong on a card.

    Node-enter PROGRESS events (`{"node": ..., "phase": "enter"}`, published by
    the LangGraph node callback) are trajectory bookkeeping: the Dashboard feed
    shows them, the mission card's short tail must not (finding #37)."""
    payload: dict[str, Any] = getattr(ev, "payload", None) or {}
    if payload.get("node") and payload.get("phase") == "enter":
        return None
    step = event_step(ev)
    if step is None:
        return None
    return str(step._head).split("\n")[0]


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
        budget = float(getattr(p, "budget_usd", 0.0) or 0.0)
        reason = str(getattr(p, "budget_reason", "") or "")
        if budget > 0:
            yield Static(self.budget_line(budget, reason), classes="card-body pc-budget")
            with Horizontal(classes="pc-budget-row"):
                # the owner can reprice before Launch: the card writes the value
                # back onto the proposal, which is what launch() reads
                yield Static(f"[{MUTED}]budget $[/]", classes="pc-budget-label")
                yield Input(
                    f"{budget:.2f}",
                    id="budget",
                    type="number",
                    compact=True,
                    classes="pc-budget-input",
                )
                yield Static(f"[{FAINT}]usd, editable[/]", classes="spacer")
        with Horizontal(classes="card-actions"):
            yield Btn("Launch", "launch", variant="primary")
            yield Btn("Scrap", "scrap", variant="ghost")
            yield Static(f"[{FAINT}]or reply with changes · “yes” launches[/]", classes="spacer")

    @staticmethod
    def budget_line(usd: float, reason: str) -> str:
        """`budget $3.00 (normal feature-sized change)`, the one line that shows
        how much the mission may spend and why."""
        line = f"[b]budget {money(usd)}[/b]"
        if reason:
            line = f"{line} [{MUTED}]({esc(reason)})[/]"
        return line

    def on_mount(self) -> None:
        self.set_live(self.live)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep the proposal's budget in step with the field. Garbage and <= 0
        are ignored: the last good value stands and nothing crashes."""
        if event.input.id != "budget":
            return
        event.stop()
        self.apply_budget(event.value)

    def apply_budget(self, value: Any) -> None:
        """Write an edited budget onto the proposal (no-op when unusable)."""
        try:
            usd = float(str(value).strip().lstrip("$"))
        except (TypeError, ValueError):
            return
        if not math.isfinite(usd) or usd <= 0:
            return
        self.proposal.budget_usd = usd
        self.proposal.budget_reason = "owner-set"

    def set_live(self, live: bool) -> None:
        self.live = live
        self.query_one(".card-actions").display = live
        for row in self.query(".pc-budget-row"):
            row.display = live  # a settled proposal is history, not a form
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
        # a finished card backfills its tail once, then is never polled again
        self.tail_filled = False

    def compose(self) -> ComposeResult:
        yield Static(f"[{MUTED}]mission {esc(self.mission_id)}[/]", classes="card-body mc-head")
        yield Static("", classes="card-body mc-tail")
        yield Static("", classes="card-body mc-gate")
        yield Static("", classes="card-body mc-pr")
        with Horizontal(classes="card-actions mc-actions"):
            yield Btn("Open", "open")
            yield Btn("Preview", "preview", classes="mc-previewbtn")
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
        """Live path: append what is new and keep the last few lines."""
        for ev in events:
            self.last_seq = max(self.last_seq, int(ev.seq or 0))
            if ev.run_id:
                self.active_role = role_of(ev.run_id)
            line = _tail_line(ev)
            if line is not None:
                self.tail.append(line)
        self.tail = self.tail[-self.TAIL :]
        self.paint()

    def rebuild_tail(self, events: list[Any]) -> None:
        """Replace the tail with the NEWEST renderable events.

        A finished mission is no longer polled (work.py `_poll_cards`), so the
        lines the card accumulated early — `coder #2 read_file` from hours ago
        — used to freeze there forever. The Work view hands this method the
        mission's event list once and the card picks the last few lines the
        same way the live path would; `last_seq` moves to the newest event so
        the caller can see it is caught up."""
        lines = [line for line in (_tail_line(ev) for ev in events) if line is not None]
        self.tail = lines[-self.TAIL :]
        if events:
            self.last_seq = max(self.last_seq, max(int(ev.seq or 0) for ev in events))
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
        pr = self.query_one(".mc-pr", Static)
        pr_url = (getattr(m, "result", None) or {}).get("pr_url")
        pr.display = bool(pr_url)
        if pr_url:
            pr.update(f"PR: {esc(pr_url)}")
        for btn in self.query(".mc-previewbtn").results(Btn):
            btn.display = bool(pr_url)
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
