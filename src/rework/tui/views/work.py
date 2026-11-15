"""Work: talk to the orchestrator (Claude Code / pi style).

Left: saved chats. Right: the conversation: your messages, the orchestrator's
tool calls and replies, proposal cards (Launch / Scrap) and live mission cards,
then the composer with the project / model / effort knobs.

Bare `rework` opens here with NO chat selected (owner requirement): typing
starts a new chat."""

from __future__ import annotations

import time
from functools import partial
from typing import Any

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static, TextArea

from rework.tui import actions
from rework.tui.conversation import (
    Conversation,
    Item,
    args_text,
    default_model,
    items_from_messages,
)
from rework.tui.hub import Snapshot
from rework.tui.theme import (
    ACCENT,
    DANGER,
    FAINT,
    MUTED,
    WARN,
    ago,
    clip,
    esc,
    glyph,
    money,
    tokens,
)
from rework.tui.views.base import View
from rework.tui.widgets.base import Btn, Label, RowData, RowList
from rework.tui.widgets.cards import MissionCard, ProposalCard
from rework.tui.widgets.modals import Option, Picker
from rework.tui.widgets.prompt import Prompt
from rework.tui.widgets.trajectory import Step, Trajectory, assistant_step, user_step

WELCOME = f"""[b]Talk to the orchestrator.[/b]

[{MUTED}]Say what you want done, like "fix the flaky test in polystats", "audit the
auth module". It looks at your projects and missions, proposes a mission, and
nothing runs until you press Launch.[/]

[{FAINT}]enter send · shift+enter newline · ctrl+n new chat · f1 keys[/]"""

EFFORTS = [("", "default"), ("low", "low"), ("medium", "medium"), ("high", "high")]


class Knob(Static):
    """A clickable `label value ▾` that opens a picker."""

    def __init__(self, knob: str) -> None:
        super().__init__("", classes="knob")
        self.knob = knob

    def on_click(self, event: events.Click) -> None:
        event.stop()
        view = self.screen.query_one(WorkView)
        view.pick(self.knob)


class WorkView(View):
    TAB = "work"
    LABEL = "Work"

    def __init__(self) -> None:
        super().__init__()
        self.convo: Conversation | None = None
        self.snap = Snapshot()
        # knobs for the NEXT new chat (and mirrored from the open chat)
        self.project_id: str | None = None
        self.model_id: str = ""
        self.effort: str = ""
        self._thinking: Step | None = None
        self._sending = False
        self._streamed: list[Step] = []
        self._loading_chat: str | None = None
        # cursor for the open transcript: the highest chat_messages.id already
        # rendered. The table is append-only, so a tick only has to ask for
        # rows newer than this (indexed, O(log n)) and append those.
        self._chat_cursor: int = 0
        # where the next message goes: None = the chat orchestrator (proposes
        # missions); a mission id = that mission's orchestrator (ADR 0007)
        self.target: str | None = None
        self._target_auto = True

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="sidebar", id="work-side"):
                with Horizontal(classes="side-actions"):
                    yield Btn("+ New chat", "new", variant="primary", tooltip="ctrl+n")
                yield Label("Chats")
                yield RowList(id="chat-list", empty=f"[{FAINT}]No chats yet.[/]")
            with Vertical(classes="main"):
                with Horizontal(classes="pane-head", id="work-head"):
                    yield Static("", id="work-title", classes="pane-head-title")
                    yield Static("", classes="spacer")
                    yield Btn("Rename", "rename", variant="ghost", id="work-rename")
                    yield Btn("Delete", "delete", variant="ghost", id="work-delete")
                yield Static(WELCOME, id="work-welcome", classes="empty")
                yield Trajectory(id="chat-log")
                with Vertical(id="composer"):
                    with Vertical(id="composer-box"):
                        yield Static("", id="composer-attach")
                        yield Prompt(id="prompt", placeholder="Message the orchestrator…")
                    with Horizontal(id="composer-knobs"):
                        yield Knob("to")
                        yield Knob("project")
                        yield Knob("model")
                        yield Knob("effort")
                        yield Static("", classes="spacer")
                        yield Static(f"[{FAINT}]enter ↵ send[/]", id="send-hint")

    def on_mount(self) -> None:
        settings = self.rw.settings
        self.model_id = settings.chat_model or ""
        self.effort = settings.chat_effort or ""
        self._show_chat(False)
        self._paint_knobs()
        self._paint_attachments()
        self.query_one("#prompt", Prompt).focus()

    def hints(self) -> list[tuple[str, str]]:
        return [("enter", "send"), ("shift+enter", "newline"), ("ctrl+n", "new chat")]

    def shown(self) -> None:
        self.focus_prompt()

    def focus_prompt(self) -> None:
        self.query_one("#prompt", Prompt).focus()

    def _show_chat(self, has_chat: bool) -> None:
        self.query_one("#work-welcome").display = not has_chat
        self.query_one("#chat-log").display = has_chat
        self.query_one("#work-rename").display = has_chat
        self.query_one("#work-delete").display = has_chat

    # -- hub -------------------------------------------------------------------

    def on_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        self._paint_chat_list()
        self._paint_head()
        self._paint_knobs()
        self._refresh_cards()
        self._poll_chat()

    def tick(self, frame: str, now: float) -> None:
        """Animate the running mission cards (10fps): no store, no remounting.

        The frame comes from the shared clock; the last event time comes from
        the snapshot the view already holds, so a poll cannot reset the spinner.
        Each card is judged by ITS mission's own stamp. A mission with no stamp
        is unknown and animates; the "*" aggregate (newest event of any running
        mission) is never borrowed, or a dead card would animate whenever any
        other mission worked."""
        cards = [c for c in self.query(MissionCard) if c.status == "running"]
        if not cards:
            return
        stamps = self.snap.last_event
        with self.painting():
            for card in cards:
                last = stamps.get(card.mission_id)
                card.tick(frame, now, last)

    def _poll_chat(self) -> None:
        """Another instance's turns are persisted to the shared store; an
        already-open transcript has to re-read them. Cheap max-id probe first,
        then only the new rows are appended (no re-mount, no full replay)."""
        convo = self.convo
        if convo is None or self._loading_chat == convo.id:
            return  # nothing open, or a load for it is still in flight
        self._tick_chat(convo.id)

    @work(exclusive=True, group="chat-poll")
    async def _tick_chat(self, chat_id: str) -> None:
        data = self.hub.data
        head = await self.hub.guard(partial(data.chat_messages_max_id, chat_id), None)
        if head is None or head <= self._chat_cursor:
            return  # None = the probe failed; <= means nothing new
        messages = await self.hub.guard(
            partial(data.chat_messages_since, chat_id, self._chat_cursor), []
        )
        # the open chat may have changed while we were away: drop the result
        if not messages or self._loading_chat == chat_id:
            return
        if self.convo is None or self.convo.id != chat_id:
            return
        self._chat_cursor = max(self._chat_cursor, *(int(m.id or 0) for m in messages))
        # rows THIS instance wrote (a streaming turn, a note) are already on
        # screen, rendered live; only foreign rows are new to this view. This
        # holds during our own turn too, so it also covers a tick that was in
        # flight while we persisted.
        own = data.own_chat_ids
        fresh = [m for m in messages if int(m.id or 0) not in own]
        own.difference_update(i for i in list(own) if i <= self._chat_cursor)
        items = items_from_messages(fresh)
        if items:
            with self.painting():  # the app can close during the awaits above
                self._render_items(items)

    def _paint_chat_list(self) -> None:
        mission_status = {m.id: m.status for m in self.snap.missions}
        rows: list[RowData] = []
        for chat in sorted(self.snap.chats, key=lambda c: -float(c.updated_at or 0)):
            title = chat.title or "untitled"
            status = mission_status.get(chat.mission_id) if chat.mission_id else None
            mark = f"{glyph(status)} " if status is not None else ""
            project = self.snap.project_name(chat.project_id)
            meta = "  ·  ".join(x for x in (project, ago(chat.updated_at)) if x)
            rows.append(
                RowData(
                    chat.id,
                    f"{mark}{esc(clip(title, 28))}\n[{FAINT}]{esc(meta)}[/]",
                )
            )
        lst = self.query_one("#chat-list", RowList)
        lst.set_rows(rows)
        lst.select(self.convo.id if self.convo else None)

    def _paint_head(self) -> None:
        title = self.query_one("#work-title", Static)
        if self.convo is None:
            title.update(f"[{MUTED}]New conversation[/]")
            return
        chat = self.convo.chat
        project = self.snap.project_name(chat.project_id) or "no project"
        title.update(
            f"[b]{esc(clip(chat.title or 'untitled', 60))}[/b]  [{FAINT}]{esc(project)}[/]"
        )

    def active_missions(self) -> list[str]:
        """This chat's missions that are still running, newest last."""
        if self.convo is None:
            return []
        ids = [m.id for m in self.snap.missions if getattr(m, "chat_id", None) == self.convo.id]
        ids += [c.mission_id for c in self.query(MissionCard) if c.mission_id not in ids]
        live = []
        for mid in ids:
            m = self.snap.mission(mid)
            if m is not None and m.status.value in actions.ACTIVE:
                live.append((m.created_at, mid))
        return [mid for _, mid in sorted(live)]

    def _resolve_target(self) -> None:
        live = self.active_missions()
        if self._target_auto:
            self.target = live[-1] if live else None
        elif self.target is not None and self.target not in live:
            self.target = None  # that mission ended; talk to the chat again

    def _paint_knobs(self) -> None:
        self._resolve_target()
        prompt = self.query_one("#prompt", Prompt)
        if self.target:
            prompt.placeholder = f"Message {self.target}'s orchestrator…"
        else:
            prompt.placeholder = "Message the orchestrator…"
        project = self.snap.project_name(self.project_id) if self.project_id else "none"
        model = self.model_id or self._default_model()
        effort = self.effort or "default"
        to = f"mission {self.target}" if self.target else "chat"
        for knob in self.query(Knob):
            if knob.knob == "to":
                knob.display = bool(self.active_missions())
            value = {"project": project, "model": model, "effort": effort, "to": to}[knob.knob]
            knob.update(f"[{FAINT}]{knob.knob}[/] {esc(value)} [{FAINT}]▾[/]")

    def _default_model(self) -> str:
        try:
            from rework.config import load_catalog

            return default_model(load_catalog())
        except Exception:
            return "?"

    def _paint_attachments(self) -> None:
        prompt = self.query_one("#prompt", Prompt)
        box = self.query_one("#composer-attach", Static)
        box.display = bool(prompt.attachments)
        if prompt.attachments:
            names = "  ".join(f"⎘ {p.rsplit('/', 1)[-1]}" for p in prompt.attachments)
            box.update(f"[{MUTED}]{esc(names)}[/]  [{FAINT}](esc clears)[/]")

    def on_prompt_attachments_changed(self, _: Prompt.AttachmentsChanged) -> None:
        self._paint_attachments()

    # -- chat list ---------------------------------------------------------------

    def on_row_list_activated(self, event: RowList.Activated) -> None:
        if event.rows.id == "chat-list":
            event.stop()
            if self.convo is None or self.convo.id != event.key:
                self.open_chat(event.key)

    def open_chat(self, chat_id: str) -> None:
        self._load_chat(chat_id)

    @work(exclusive=True, group="load-chat")
    async def _load_chat(self, chat_id: str) -> None:
        self._loading_chat = chat_id
        try:
            await self._read_chat(chat_id)
        finally:
            # `_loading_chat` means "a load is in flight"; leaving it set made
            # the transcript poller think a load was still running forever
            if self._loading_chat == chat_id:
                self._loading_chat = None

    async def _read_chat(self, chat_id: str) -> None:
        self._chat_cursor = 0  # never carry a cursor across chats
        chat = await self.hub.guard(lambda: self.hub.data.chat(chat_id), None)
        if chat is None:
            self.toast("Chat not found", severity="warning")
            return
        convo = Conversation(self.hub.data, chat)
        # the cursor comes from the rows actually rendered, so a row committed
        # while we read can never fall below it unseen
        messages = await self.hub.guard(partial(self.hub.data.chat_messages, chat_id), [])
        items = items_from_messages(messages)
        cursor = max((int(m.id or 0) for m in messages), default=0)
        if self._loading_chat != chat_id:
            return
        self.convo = convo
        self._chat_cursor = cursor
        self._target_auto = True
        self.project_id = chat.project_id
        self.model_id = chat.model_id or self.model_id
        self.effort = chat.effort or ""
        # the app can close during the awaits above: guard the paint only
        with self.painting():
            log = self.query_one("#chat-log", Trajectory)
            log.clear()
            self._show_chat(True)
            self._render_items(items, replay=True)
            self._paint_head()
            self._paint_knobs()
            self._paint_chat_list()
            self._refresh_cards()
            self.call_after_refresh(log.scroll_end, animate=False)
            self.focus_prompt()

    def new_chat(self) -> None:
        self.convo = None
        self._chat_cursor = 0
        self._target_auto = True
        self.target = None
        self.query_one("#chat-log", Trajectory).clear()
        self._show_chat(False)
        self._paint_head()
        self._paint_chat_list()
        self.focus_prompt()

    # -- rendering ------------------------------------------------------------------

    def _render_items(self, items: list[Item], *, replay: bool = False) -> None:
        log = self.query_one("#chat-log", Trajectory)
        widgets: list[Any] = []
        last_proposal = None
        launched_after = False
        for item in items:
            if item.kind == "proposal":
                last_proposal = item
                launched_after = False
            elif item.kind == "mission":
                launched_after = True
        for item in items:
            widget = self._item_widget(item)
            if widget is None:
                continue
            if isinstance(widget, ProposalCard):
                widget.live = item is last_proposal and not launched_after
            widgets.append(widget)
        log.add(*widgets)

    def _item_widget(self, item: Item) -> Any:
        if item.kind == "user":
            return user_step(item.text, item.attachments)
        if item.kind == "assistant":
            return assistant_step(item.text)
        if item.kind == "tool_call":
            return Step(
                f"[{ACCENT}]⏺[/] [b]{esc(item.name)}[/b]  [{MUTED}]{esc(clip(item.args, 140))}[/]",
                kind="call",
            )
        if item.kind == "tool_result":
            colour = DANGER if item.error else MUTED
            lines = item.text.strip().splitlines() or ["(empty)"]
            more = f" [{FAINT}]+{len(lines) - 1} lines[/]" if len(lines) > 1 else ""
            detail = "\n".join(f"    [{colour}]{esc(line)}[/]" for line in lines[:40])
            return Step(
                f"  [{FAINT}]⎿[/] [{colour}]{esc(clip(lines[0], 140))}[/]{more}",
                detail=detail if len(lines) > 1 else "",
                kind="result",
            )
        if item.kind == "proposal":
            return ProposalCard(item.proposal)
        if item.kind == "mission":
            return MissionCard(item.mission_id)
        if item.kind == "note":
            return Step(f"[{FAINT}]{esc(item.text)}[/]", kind="note")
        return None

    def _retire_proposals(self) -> None:
        for card in self.query(ProposalCard):
            card.set_live(False)

    # -- sending ----------------------------------------------------------------------

    def on_prompt_submitted(self, event: Prompt.Submitted) -> None:
        event.stop()
        if self._sending:
            self.toast("Still waiting for the orchestrator…", severity="warning")
            return
        self._resolve_target()
        if self.target:
            self._message_mission(self.target, event.text)
            return
        self._send(event.text, event.attachments)

    @work(group="send")
    async def _message_mission(self, mission_id: str, text: str) -> None:
        prompt = self.query_one("#prompt", Prompt)
        log = self.query_one("#chat-log", Trajectory)
        try:
            await self.hub.data.send_owner_message(mission_id, text)
        except Exception as exc:
            self.toast(f"Could not reach the mission: {clip(str(exc), 200)}", severity="error")
            return
        prompt.accept()
        log.add(user_step(text))
        log.add(Step(f"  [{FAINT}]→ sent to {esc(mission_id)}'s orchestrator[/]", kind="meta"))
        if self.convo is not None:
            try:
                await self.hub.data.append_chat_note(self.convo.id, f"you → {mission_id}: {text}")
            except Exception:
                pass

    @work(group="send")
    async def _send(self, text: str, attachments: list[str]) -> None:
        prompt = self.query_one("#prompt", Prompt)
        log = self.query_one("#chat-log", Trajectory)
        self._sending = True
        try:
            if self.convo is None:
                chat = await self.hub.data.new_chat(
                    title=clip(text or "image", 60),
                    project_id=self.project_id,
                    model_id=self.model_id or self._default_model(),
                    effort=self.effort or None,
                )
                self.convo = Conversation(self.hub.data, chat)
                log.clear()
                self._show_chat(True)
                self._paint_head()
            convo = self.convo
            engine = await convo.ensure_engine(self.snap, self._on_turn)
            if attachments and not engine.supports_images:
                self.toast(
                    f"{engine.model_label} can't read images. Switch model or remove the "
                    "attachment",
                    severity="error",
                )
                return
            prompt.accept()
            log.add(user_step(text, attachments))
            self._thinking = Step(f"[{ACCENT}]●[/] [{MUTED}]thinking…[/]", kind="note")
            self._streamed = []
            log.add(self._thinking)
            started = time.monotonic()
            try:
                result = await engine.send(text, images=attachments or None)
            except Exception as exc:
                self._clear_thinking()
                log.add(
                    Step(
                        f"[{DANGER}]✗ {esc(type(exc).__name__)}: {esc(clip(str(exc), 400))}[/]",
                        kind="error",
                    )
                )
                prompt.text = text  # never lose input
                return
            self._clear_thinking()
            if result.reply.startswith("⚠ "):
                # the turn failed inside the engine (provider down, etc.): the
                # error line is in the chat; give the owner their text back
                prompt.text = text  # never lose input
                return
            if engine.launched and engine.proposal is not None:
                await self._launch(engine.proposal)
                return
            if result.proposal is not None:
                self._retire_proposals()
                log.add(assistant_step(result.reply or "Here's what I'd run."))
                log.add(ProposalCard(result.proposal))
            elif result.reply:
                if result.reply.startswith("Scrapped"):
                    self._retire_proposals()
                log.add(assistant_step(result.reply))
            elapsed = time.monotonic() - started
            if result.turn_tokens:
                log.add(
                    Step(
                        f"  [{FAINT}]{tokens(result.turn_tokens)} tok · "
                        f"{money(result.turn_cost_usd)} · {elapsed:.1f}s[/]",
                        kind="meta",
                    )
                )
            await self.hub.refresh()
        except Exception as exc:
            self._clear_thinking()
            self.toast(f"Send failed: {clip(str(exc), 200)}", severity="error")
        finally:
            self._sending = False

    def _clear_thinking(self) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None

    def _on_turn(self, kind: str, payload: dict[str, Any]) -> None:
        """Live trajectory from the orchestrator's tool loop."""
        try:
            log = self.query_one("#chat-log", Trajectory)
            step: Step | None = None
            if kind == "tool_call":
                step = self._item_widget(
                    Item(
                        "tool_call",
                        name=str(payload.get("name") or ""),
                        args=args_text(payload.get("arguments")),
                    )
                )
            elif kind == "tool_result":
                step = self._item_widget(
                    Item(
                        "tool_result",
                        name=str(payload.get("name") or ""),
                        text=str(payload.get("result") or ""),
                        error=str(payload.get("error") or ""),
                    )
                )
            elif kind == "turn" and payload.get("text"):
                step = assistant_step(str(payload["text"]))
            if step is None:
                return
            if self._thinking is not None:
                # keep "thinking" as the last line while tools run
                log.mount(step, before=self._thinking)
            else:
                log.add(step)
        except Exception:
            pass

    # -- proposals & launch -------------------------------------------------------------

    def on_proposal_card_decided(self, event: ProposalCard.Decided) -> None:
        event.stop()
        if self._sending:
            return
        if event.launch:
            self._launch_worker(event.card.proposal)
        else:
            event.card.set_live(False)
            if self.convo and self.convo.engine is not None:
                self.convo.engine.proposal = None
            self.query_one("#chat-log", Trajectory).add(
                Step(f"[{FAINT}]proposal scrapped. Tell the orchestrator what to change[/]")
            )

    @work(group="send")
    async def _launch_worker(self, proposal: Any) -> None:
        self._sending = True
        try:
            await self._launch(proposal)
        finally:
            self._sending = False

    async def _launch(self, proposal: Any) -> None:
        log = self.query_one("#chat-log", Trajectory)
        if self.convo is None:
            return
        try:
            mission = await self.convo.launch(self.snap, proposal)
        except ValueError as exc:
            log.add(
                Step(
                    f"[{WARN}]◆[/] {esc(str(exc))}\n  [{MUTED}]Reply with a git URL or a "
                    "folder path and the orchestrator will re-propose.[/]",
                    kind="gate",
                )
            )
            return
        except Exception as exc:
            log.add(Step(f"[{DANGER}]✗ launch failed: {esc(clip(str(exc), 300))}[/]"))
            return
        self._retire_proposals()
        card = MissionCard(mission.id)
        log.add(card)
        self.toast(f"Mission {mission.id} launched", title="Launched")
        await self.hub.refresh()

    # -- live mission cards -------------------------------------------------------------

    def _refresh_cards(self) -> None:
        cards = list(self.query(MissionCard))
        if not cards:
            return
        for card in cards:
            mission = self.snap.mission(card.mission_id)
            if mission is not None:
                card.update_from(mission, self.snap.gates_for(card.mission_id))
        self._poll_cards(cards)

    @work(exclusive=True, group="cards")
    async def _poll_cards(self, cards: list[MissionCard]) -> None:
        for card in cards:
            if card.mission is None:
                mission = await self.hub.guard(
                    partial(self.hub.data.get_mission, card.mission_id), None
                )
                if mission is not None:
                    with self.painting():  # the app can close during the await above
                        card.update_from(mission, self.snap.gates_for(card.mission_id))
            if card.status not in actions.ACTIVE:
                # Finished (done/failed/cancelled): nothing more will arrive,
                # so the card is never polled again. Whatever lines it had
                # accumulated early would freeze there (finding #37), so
                # backfill the tail ONCE from the mission's newest events and
                # then stop, exactly as before.
                if not card.tail_filled:
                    # `mission_events` pages FORWARD from seq 0, so on a long
                    # mission it hands back the oldest window; ask for the
                    # newest events instead. None = the fetch failed (guard's
                    # default): keep the card unfilled so the next poll retries
                    # rather than freezing an empty tail forever.
                    evs = await self.hub.guard(
                        partial(self.hub.data.mission_events_newest, card.mission_id, 5000), None
                    )
                    if evs is not None:
                        with self.painting():
                            card.rebuild_tail(evs)
                        card.tail_filled = True
                    # its cost too, once: the finished path skips the live
                    # poll below (found live: a done card lost its $7.07)
                    card.spend = await self.hub.guard(
                        partial(self.hub.data.mission_spend, card.mission_id), card.spend
                    )
                    with self.painting():
                        card.paint()
                continue
            evs = await self.hub.guard(
                partial(self.hub.data.events_tail, card.mission_id, card.last_seq), []
            )
            if evs:
                with self.painting():
                    card.add_events(evs)
            card.spend = await self.hub.guard(
                partial(self.hub.data.mission_spend, card.mission_id), card.spend
            )
            with self.painting():
                card.paint()

    def on_mission_card_act(self, event: MissionCard.Act) -> None:
        event.stop()
        verb = event.verb
        mid = event.card.mission_id
        if verb == "open":
            self.rw.open_mission(mid)
        elif verb in ("approve", "reject") and event.gate_id:
            self.run_worker(
                actions.decide(self.rw, event.gate_id, verb == "approve"), group="decide"
            )
        elif verb == "answer" and event.gate_id:
            self.rw.action_tab("gates")
            self.rw.view("gates").select_gate(event.gate_id)
        elif verb == "preview":
            self.run_worker(actions.preview(self.rw, mid), group="preview")
        elif verb in ("pause", "resume", "cancel"):
            self.run_worker(actions.control(self.rw, mid, verb), group="control")

    def last_mission_id(self) -> str | None:
        cards = list(self.query(MissionCard))
        return cards[-1].mission_id if cards else None

    # -- header buttons & knobs -------------------------------------------------------

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        if event.action == "new":
            event.stop()
            self.new_chat()
        elif event.action == "rename" and self.convo:
            event.stop()
            self._rename()
        elif event.action == "delete" and self.convo:
            event.stop()
            self._confirm_delete(event.btn)

    def _rename(self) -> None:
        from rework.tui.widgets.modals import Field, Form

        assert self.convo is not None
        convo = self.convo

        def done(values: dict[str, str] | None) -> None:
            if values and values.get("title", "").strip():
                self.run_worker(self._do_rename(convo, values["title"].strip()))

        self.rw.push_screen(
            Form("Rename chat", [Field("title", "Title", value=convo.chat.title or "")]), done
        )

    async def _do_rename(self, convo: Conversation, title: str) -> None:
        chat = await self.hub.data.rename_chat(convo.id, title)
        if chat is not None:
            convo.chat = chat
        await self.hub.refresh()

    def _confirm_delete(self, btn: Btn) -> None:
        if btn.action == "delete" and not btn.has_class("-armed"):
            btn.add_class("-armed")
            btn.set_label("Really delete?")
            btn.remove_class("ghost")
            btn.add_class("danger")
            self.set_timer(3, lambda: self._disarm(btn))
            return
        self._disarm(btn)
        self.run_worker(self._do_delete())

    def _disarm(self, btn: Btn) -> None:
        btn.remove_class("-armed")
        btn.set_label("Delete")
        btn.remove_class("danger")
        btn.add_class("ghost")

    async def _do_delete(self) -> None:
        if self.convo is None:
            return
        await self.hub.data.delete_chat(self.convo.id)
        self.new_chat()
        await self.hub.refresh()

    def pick(self, knob: str) -> None:
        if knob == "to":
            options = [Option("", "chat", "the chat orchestrator: propose new missions")]
            for mid in reversed(self.active_missions()):
                m = self.snap.mission(mid)
                options.append(Option(mid, f"mission {mid}", clip(m.objective, 50) if m else ""))
            current = self.target or ""

            def chose(value: str | None) -> None:
                if value is None:
                    return
                self._target_auto = False
                self.target = value or None
                self._paint_knobs()
                self.focus_prompt()

            self.rw.push_screen(Picker("Send messages to", options, current), chose)
            return
        if knob == "project":
            options = [Option("", "No project", "missions can be project-less")]
            for p in self.snap.projects:
                repos = (p.settings or {}).get("repos") or []
                options.append(Option(p.id, p.name, f"{len(repos)} repo(s)"))
            current = self.project_id or ""
        elif knob == "model":
            try:
                from rework.config import load_catalog

                entries = load_catalog().all()
            except Exception as exc:
                self.toast(f"Model catalog unreadable: {exc}", severity="error")
                return
            options = []
            for e in entries:
                ready = e.has_credentials()
                options.append(
                    Option(
                        e.id,
                        e.id,
                        f"{e.model} · tier {e.tier} · ${e.cost_in_per_mtok}/"
                        f"${e.cost_out_per_mtok}"
                        + (" · vision" if "vision" in e.capabilities else "")
                        + ("" if ready else " · no key"),
                        # dimmed, not hidden: the owner can still pick it and go
                        # add the key (the orchestrator rejects it with a clear
                        # error if it really has no key by then)
                        classes="" if ready else "-dim",
                    )
                )
            options.sort(key=lambda o: bool(o.classes))  # keyed models first
            current = self.model_id or self._default_model()
        else:
            options = [Option(v, label) for v, label in EFFORTS]
            current = self.effort

        def done(value: str | None) -> None:
            if value is None:
                return
            self.run_worker(self._apply_knob(knob, value))

        self.rw.push_screen(Picker(f"Choose {knob}", options, current), done)

    async def _apply_knob(self, knob: str, value: str) -> None:
        if knob == "project":
            self.project_id = value or None
        elif knob == "model":
            self.model_id = value
            self.rw.settings.chat_model = value
            self.rw.settings.save()
        else:
            self.effort = value
            self.rw.settings.chat_effort = value
            self.rw.settings.save()
        self._paint_knobs()
        if self.convo is not None:
            field = {"project": "project_id", "model": "model_id", "effort": "effort"}[knob]
            try:
                chat = await self.hub.data.update_chat(self.convo.id, **{field: value or None})
                if chat is not None:
                    self.convo.chat = chat
            except Exception as exc:
                self.toast(f"Could not save: {exc}", severity="error")
            self._paint_head()
        self.focus_prompt()

    def on_key(self, event: events.Key) -> None:
        """Typing goes to the prompt wherever focus is in this tab. Found
        driving the TUI: after a click in the transcript (to select text, or a
        mis-click) keystrokes went to the scroll view and vanished."""
        prompt = self.query_one("#prompt", Prompt)
        if self.screen.focused is prompt or not event.is_printable or not event.character:
            return
        if isinstance(self.screen.focused, Input | TextArea):
            return  # another text box (a dialog field) keeps its keys
        event.stop()
        prompt.focus()
        prompt.insert(event.character)

    def back(self) -> bool:
        prompt = self.query_one("#prompt", Prompt)
        if prompt.attachments:
            prompt.clear_attachments()
            return True
        return False
