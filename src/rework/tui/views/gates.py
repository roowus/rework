"""Gates: where agents need you. The most time-sensitive surface.

Left: the inbox (open gates, oldest first, so the one blocking longest is on top) or
the decided history. Right: the selected gate with everything needed to decide:
the question, its mission, the run it unblocks, the diff for ship gates, the
agent's recent steps, and an answer box. Approve / Reject are buttons first,
alt+a / alt+x second; a typed answer rides along to the paused role."""

from __future__ import annotations

from functools import partial
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from rework.tui import actions
from rework.tui.hub import Snapshot
from rework.tui.theme import (
    FAINT,
    MUTED,
    OK,
    WARN,
    ago,
    clip,
    esc,
    glyph,
    mission_name,
    status_text,
)
from rework.tui.views.base import View
from rework.tui.widgets.artifacts import (
    artifact_markup,
    diff_markup,
    escalation_markup,
)
from rework.tui.widgets.base import Btn, ChipGroup, Empty, Label, RowData, RowList
from rework.tui.widgets.trajectory import event_step, role_of

# (approve label, reject label) per gate kind: buttons speak the gate's language
ACTION_LABELS = {
    "ship": ("Ship it", "Don't ship"),
    "open_pr": ("Open the PR", "Don't open"),
    "spawn": ("Allow", "Deny"),
    "permission": ("Allow", "Deny"),
    "task": ("Done", "Can't do it"),
    "generic": ("Approve", "Reject"),
    "question": ("Send answer", "Decline"),
}

KIND_LABEL = {
    "ship": "ship it?",
    "open_pr": "open a PR?",
    "spawn": "spawn a role?",
    "generic": "question",
    "question": "question",
    "choice": "choose",
    "task": "do this",
    "permission": "permission",
}


class GatesView(View):
    TAB = "gates"
    LABEL = "Gates"

    BINDINGS: ClassVar[list[BindingType]] = [
        # alt+a / alt+x in meta-mode terminals (priority so a focused answer box
        # can't eat them); the macOS å / ≈ spellings are handled by the App
        Binding("alt+a", "decide(True)", "approve", show=False, priority=True),
        Binding("alt+x", "decide(False)", "reject", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.snap = Snapshot()
        self.mode = "open"
        self.selected: Any = None
        self.history: list[Any] = []
        self._detail_for: str | None = None
        # newest event seq already rendered in the detail pane's steps box.
        # events_since(seq) is the indexed monotonic cursor: asking from 0 (the
        # old behaviour) pages FORWARD, so a long mission froze on its OLDEST
        # 500 events (finding #37, same trap the mission cards had).
        self._steps_seq: int = 0
        self._steps_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="sidebar wide", id="gates-side"):
                with Horizontal(classes="side-actions"):
                    yield ChipGroup(
                        [("Open", "open"), ("Decided", "decided")], "open", id="gate-mode"
                    )
                yield RowList(id="gate-list", empty=f"[{FAINT}]Nothing needs you. ✓[/]")
            with Vertical(classes="main"):
                yield Empty(
                    f"[{OK}]✓[/] [b]Nothing is waiting on you.[/b]\n\n"
                    f"[{FAINT}]Gates appear here when an agent needs a decision, an answer,\n"
                    "or your permission. You'll get a notification.[/]",
                    id="gate-empty",
                )
                with Vertical(id="gate-pane"):
                    with Horizontal(classes="pane-head"):
                        yield Static("", id="gate-head", classes="pane-head-title")
                        yield Static("", classes="spacer")
                        yield Btn("Open mission", "open-mission", variant="ghost")
                    with VerticalScroll(id="gate-detail"):
                        yield Static("", id="gate-question")
                        yield Label("Mission")
                        yield Static("", id="gate-mission", classes="section")
                        yield Label("Unblocks")
                        yield Static("", id="gate-run", classes="section")
                        yield Label("Changes", id="gate-diff-label")
                        yield Static("", id="gate-diff", classes="section")
                        yield Label("Recent steps", id="gate-steps-label")
                        yield Static("", id="gate-steps", classes="section")
                        yield Static("", id="gate-decided", classes="section")
                    with Vertical(id="gate-answer"):
                        yield Horizontal(id="gate-choices")
                        yield TextArea(
                            "",
                            id="gate-text",
                            compact=True,
                            placeholder="Optional answer or feedback (the agent receives it)",
                        )
                        with Horizontal(classes="gate-actions"):
                            yield Btn("Approve  alt+a", "approve", variant="primary")
                            yield Btn("Reject  alt+x", "reject", variant="danger")
                            yield Static("", classes="spacer")
                            yield Static(
                                f"[{FAINT}]a reject with feedback resumes the agent[/]",
                                classes="gate-tip",
                            )

    def on_mount(self) -> None:
        self._paint_detail()

    def hints(self) -> list[tuple[str, str]]:
        return [("alt+a", "approve"), ("alt+x", "reject"), ("click", "select")]

    def shown(self) -> None:
        if self.selected is None:
            rows = self.query_one("#gate-list", RowList)
            if rows.keys:
                rows.activate(rows.keys[0])
        self.query_one("#gate-list", RowList).focus()

    # -- data --------------------------------------------------------------

    def on_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        if self.mode == "open":
            self._paint_list(sorted(snap.gates, key=lambda g: float(g.created_at or 0)))
            if self.selected is not None and self.selected.status.value == "open":
                still = [g for g in snap.gates if g.id == self.selected.id]
                if not still and snap.loaded:
                    # decided elsewhere (another machine, the CLI, a chat card)
                    self.selected = None
                    self._paint_detail()
        else:
            # decided gates are not in the snapshot: re-read the history list so
            # a gate decided by another instance stops reading as open
            self._load_history()
        if self.selected is not None:
            # cheap re-paint of the selected gate's head/state (a gate decided in
            # another instance flips from waiting to answered here) plus the new
            # steps only: the runs/diff read stays on selection, it never changes
            self._paint_detail()
            self._poll_steps(self.selected)

    def _paint_list(self, gates: list[Any]) -> None:
        rows = []
        for g in gates:
            mission = self.snap.mission(g.mission_id)
            where = mission_name(mission, 40) if mission else g.mission_id
            kind = KIND_LABEL.get(g.kind.value, g.kind.value)
            mark = glyph(g.status.value)
            rows.append(
                RowData(
                    g.id,
                    f"{mark} [{MUTED}]{esc(kind)}[/]  [{FAINT}]{esc(ago(g.created_at))}[/]\n"
                    f"{esc(clip(g.question, 80))}\n[{FAINT}]{esc(where)}[/]",
                )
            )
        lst = self.query_one("#gate-list", RowList)
        lst.set_rows(rows)
        lst.select(self.selected.id if self.selected is not None else None)

    def on_chip_group_changed(self, event: ChipGroup.Changed) -> None:
        event.stop()
        self.mode = event.value
        self.selected = None
        self._paint_detail()
        if self.mode == "open":
            self.on_snapshot(self.snap)
        else:
            self._load_history()

    @work(exclusive=True, group="gate-history")
    async def _load_history(self) -> None:
        gates = await self.hub.guard(lambda: self.hub.data.gates(), [])
        fresh = sorted(
            [g for g in gates if g.status.value != "open"],
            key=lambda g: -float(g.decided_at or g.created_at or 0),
        )[:200]
        if [g.id for g in fresh] == [g.id for g in self.history]:
            return  # nothing new: repainting would only flicker (RowList.set_rows)
        self.history = fresh
        if self.mode == "decided":
            self._paint_list(self.history)

    def select_gate(self, gate_id: str) -> None:
        """Called from elsewhere (a chat card's Answer…)."""
        if self.mode != "open":
            self.query_one("#gate-mode", ChipGroup).select("open")
            self.mode = "open"
            self.on_snapshot(self.snap)
        self.query_one("#gate-list", RowList).activate(gate_id)
        self.query_one("#gate-text", TextArea).focus()

    def on_row_list_activated(self, event: RowList.Activated) -> None:
        event.stop()
        pool = self.snap.gates if self.mode == "open" else self.history
        gate = next((g for g in pool if g.id == event.key), None)
        if gate is None:
            return
        if self.selected is None or self.selected.id != gate.id:
            self.query_one("#gate-text", TextArea).text = ""
        self.selected = gate
        self._paint_detail()
        self._load_detail(gate)

    # -- detail ------------------------------------------------------------

    def _paint_detail(self) -> None:
        gate = self.selected
        self.query_one("#gate-empty").display = gate is None
        self.query_one("#gate-pane").display = gate is not None
        if gate is None:
            empty = self.query_one("#gate-empty", Empty)
            if self.mode == "decided":
                empty.update(f"[{FAINT}]Pick a decided gate to see what was answered.[/]")
            return
        kind = KIND_LABEL.get(gate.kind.value, gate.kind.value)
        state = (
            f"[{WARN}]◆ waiting {esc(ago(gate.created_at).replace(' ago', ''))}[/]"
            if gate.status.value == "open"
            else status_text(gate.status.value)
        )
        self.query_one("#gate-head", Static).update(
            f"[b]{esc(kind[:1].upper() + kind[1:])}[/b]  {state}  [{FAINT}]{esc(gate.id)}[/]"
        )
        spec = getattr(gate, "spec", None) or {}
        extra = ""
        if spec.get("detail"):
            extra += f"\n[{MUTED}]{esc(spec['detail'])}[/]"
        if spec.get("asked_by") == "orchestrator":
            extra += f"\n[{FAINT}]asked by the mission's orchestrator[/]"
        self.query_one("#gate-question", Static).update(f"[b]{esc(gate.question)}[/b]{extra}")
        is_open = gate.status.value == "open"
        self.query_one("#gate-answer").display = is_open
        self._paint_choices(gate if is_open else None)
        yes, no = ACTION_LABELS.get(gate.kind.value, ("Approve", "Reject"))
        for btn in self.query("#gate-answer Btn").results(Btn):
            if btn.action == "approve":
                btn.set_label(f"{yes}  alt+a")
            elif btn.action == "reject":
                btn.set_label(f"{no}  alt+x")
        decided = self.query_one("#gate-decided", Static)
        decided.display = not is_open
        if not is_open:
            note = gate.decision_note or ""
            decision = getattr(gate, "decision", None) or {}
            answer = decision.get("answer") or decision.get("feedback") or ""
            decided.update(
                f"\n{status_text(gate.status.value)} {esc(ago(gate.decided_at))}"
                + (f"\n[{MUTED}]note[/] {esc(note)}" if note else "")
                + (f"\n[{MUTED}]answer[/] {esc(answer)}" if answer else "")
            )
        mission = self.snap.mission(gate.mission_id)
        self._paint_mission(mission, gate)

    def _paint_mission(self, mission: Any, gate: Any) -> None:
        box = self.query_one("#gate-mission", Static)
        if mission is None:
            box.update(f"[{FAINT}]{esc(gate.mission_id)}[/]")
            return
        project = self.snap.project_name(mission.project_id) or "no project"
        box.update(
            f"{esc(mission.objective)}\n"
            f"{status_text(mission.status)}  [{FAINT}]{esc(mission.id)} · {esc(project)} · "
            f"{esc((mission.plan or {}).get('kind', ''))} · "
            f"started {esc(ago(mission.created_at))}[/]"
        )

    @work(exclusive=True, group="gate-detail")
    async def _load_detail(self, gate: Any) -> None:
        data = self.hub.data
        self._detail_for = gate.id
        self._steps_seq = 0
        self._steps_lines = []
        runs = await self.hub.guard(lambda: data.mission_runs(gate.mission_id), [])
        if self.selected is None or self.selected.id != gate.id:
            return
        run = next((r for r in runs if r.id == gate.for_run), None)
        run_box = self.query_one("#gate-run", Static)
        if run is not None:
            model = (run.model_ref or {}).get("id") or "default model"
            esc_line = escalation_markup(run.escalation)
            run_box.update(
                f"[b]{esc(role_of(run.id))}[/b]  [{FAINT}]step {esc(run.step)} · "
                f"{esc(model)} · {esc(run.status.value)}[/]" + (f"\n{esc_line}" if esc_line else "")
            )
        elif gate.kind.value == "open_pr":
            run_box.update(
                f"[{MUTED}]the whole mission: approving pushes the branch and opens a pull "
                "request (you still merge)[/]"
            )
        elif gate.kind.value == "ship":
            run_box.update(
                f"[{MUTED}]the whole mission: approving ships the result (git projects: the "
                "branch is pushed)[/]"
            )
        else:
            run_box.update(f"[{FAINT}]{esc(gate.for_run or 'mission-level gate')}[/]")

        # what would ship: the latest coder diff + the auditor's verdict
        diff_label = self.query_one("#gate-diff-label")
        diff_box = self.query_one("#gate-diff", Static)
        coder = [r for r in runs if r.role == "coder" and r.artifact and r.artifact.get("diff")]
        auditor = [r for r in runs if r.role == "auditor" and r.artifact]
        show_diff = bool(coder) and gate.kind.value in ("ship", "open_pr", "generic")
        diff_label.display = diff_box.display = show_diff or bool(auditor)
        if show_diff or auditor:
            parts = []
            if auditor:
                parts.append(artifact_markup("auditor", auditor[-1].artifact))
            if coder:
                art = coder[-1].artifact or {}
                parts.append(
                    f"[{MUTED}]branch[/] {esc(art.get('branch', ''))}\n\n"
                    + diff_markup(str(art.get("diff") or ""), max_lines=400)
                )
            diff_box.update("\n\n".join(parts))

        # one bounded window of the NEWEST events (asking from seq 0 pages
        # FORWARD, so a long mission used to freeze on its OLDEST 500 lines:
        # finding #37). Later ticks only ask for what arrived after this.
        events = await self.hub.guard(partial(data.mission_events_newest, gate.mission_id, 500), [])
        if self.selected is None or self.selected.id != gate.id:
            return
        self._steps_seq = max((int(e.seq) for e in events), default=0)
        self._steps_lines = self._step_lines(gate, events)[-12:]
        self._paint_steps()

    def _step_lines(self, gate: Any, events: list[Any]) -> list[str]:
        lines = []
        for ev in events:
            if gate.for_run and ev.run_id not in (gate.for_run, None):
                continue
            step = event_step(ev)
            if step is not None:
                lines.append(str(step._head).split("\n")[0])
        return lines

    def _paint_steps(self) -> None:
        steps_box = self.query_one("#gate-steps", Static)
        self.query_one("#gate-steps-label").display = steps_box.display = bool(self._steps_lines)
        steps_box.update("\n".join(self._steps_lines))

    @work(exclusive=True, group="gate-steps")
    async def _poll_steps(self, gate: Any) -> None:
        """Only the events that arrived since the last look: bounded, indexed
        by seq, and it appends instead of rebuilding the pane."""
        events = await self.hub.guard(
            partial(self.hub.data.events_tail, gate.mission_id, self._steps_seq, 500), []
        )
        if not events or self.selected is None or self.selected.id != gate.id:
            return
        self._steps_seq = max(self._steps_seq, max(int(e.seq) for e in events))
        fresh = self._step_lines(gate, events)
        if not fresh:
            return
        self._steps_lines = (self._steps_lines + fresh)[-12:]
        self._paint_steps()

    def _paint_choices(self, gate: Any) -> None:
        box = self.query_one("#gate-choices", Horizontal)
        box.remove_children()
        options = ((getattr(gate, "spec", None) or {}).get("options") or []) if gate else []
        is_choice = gate is not None and gate.kind.value == "choice" and options
        box.display = bool(is_choice)
        if is_choice:
            box.mount_all(
                [Btn(str(opt), "choice", variant="primary", payload=str(opt)) for opt in options]
            )
        for btn in self.query("#gate-answer .gate-actions Btn").results(Btn):
            if btn.action == "approve":
                btn.display = not is_choice  # a choice is answered by its buttons
        area = self.query_one("#gate-text", TextArea)
        placeholder = (getattr(gate, "spec", None) or {}).get("placeholder") if gate else None
        if gate is not None and gate.kind.value == "question":
            area.placeholder = placeholder or "Your answer (required)"
        elif gate is not None and gate.kind.value == "task":
            area.placeholder = "Optional note about what you did"
        else:
            area.placeholder = "Optional answer or feedback (the agent receives it)"

    # -- deciding -----------------------------------------------------------

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        if event.action == "choice" and self.selected is not None:
            event.stop()
            text = self.query_one("#gate-text", TextArea).text.strip()
            decision: dict[str, Any] = {"choice": str(event.btn.payload)}
            if text:
                decision["note"] = text
            self._decide(self.selected, True, text or str(event.btn.payload), decision)
            return
        if event.action in ("approve", "reject"):
            event.stop()
            self.action_decide(event.action == "approve")
        elif event.action == "open-mission" and self.selected is not None:
            event.stop()
            self.rw.open_mission(self.selected.mission_id, self.selected.for_run)

    def action_decide(self, approved: bool) -> None:
        gate = self.selected
        if gate is None or gate.status.value != "open":
            self.toast("Select an open gate first", severity="warning")
            return
        text = self.query_one("#gate-text", TextArea).text.strip()
        kind = gate.kind.value
        decision: dict[str, Any] | None = None
        if kind == "question" and approved and not text:
            self.toast("Type your answer first", severity="warning")
            self.query_one("#gate-text", TextArea).focus()
            return
        if kind == "task":
            decision = {"done": approved, "note": text}
        elif text:
            decision = {"answer": text} if approved else {"feedback": text}
        self._decide(gate, approved, text or None, decision)

    @work(exclusive=True, group="decide")
    async def _decide(
        self, gate: Any, approved: bool, note: str | None, decision: dict[str, Any] | None
    ) -> None:
        ok = await actions.decide(self.rw, gate.id, approved, note=note, decision=decision)
        if not ok:
            return
        self.query_one("#gate-text", TextArea).text = ""
        # move to the next waiting gate (the inbox empties itself)
        lst = self.query_one("#gate-list", RowList)
        remaining = [k for k in lst.keys if k != gate.id]
        self.selected = None
        if remaining:
            lst.activate(remaining[0])
        else:
            self._paint_detail()
