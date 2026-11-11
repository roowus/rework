"""Library: search-first. Type to search (hybrid when embeddings are on),
filter by type and status, click a result to read it, activate or retire it,
or add a new item."""

from __future__ import annotations

from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Input, Markdown, Static

from rework.tui.hub import Snapshot
from rework.tui.theme import (
    ACCENT,
    FAINT,
    MUTED,
    OK,
    WARN,
    ago,
    clip,
    esc,
)
from rework.tui.views.base import View
from rework.tui.widgets.base import Btn, ChipGroup, Empty, RowData, RowList
from rework.tui.widgets.modals import Field, Form

TYPE_GLYPH = {"skill": "◇", "tool": "⚙", "report": "▤", "memory": "◎"}
STATUS_COLOUR = {"active": OK, "proposed": WARN, "retired": FAINT}


def _v(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


class LibraryView(View):
    TAB = "library"
    LABEL = "Library"

    def __init__(self) -> None:
        super().__init__()
        self.query_text = ""
        self.type_filter = "all"
        self.status_filter = "active"
        self.items: list[Any] = []
        self.selected: Any = None
        self._debounce: Timer | None = None
        self._loaded = False
        self.mode = ""
        # cheap fingerprints of what is on screen, so the heartbeat only
        # repaints when something actually changed (no flicker, no lost input)
        self._fingerprint: tuple[Any, ...] | None = None
        self._body_fingerprint: tuple[str, int] | None = None
        self._searching = False
        self._search_epoch = 0  # bumped by every user search; a stale recheck drops

    def compose(self) -> ComposeResult:
        with Vertical(classes="pane-head lib-head"):
            with Horizontal(classes="lib-search-row"):
                yield Input(
                    placeholder="Search the library: skills, tools, reports, memories…",
                    id="lib-search",
                )
                yield Btn("+ New item", "new", variant="primary")
            with Horizontal(classes="lib-filters"):
                yield ChipGroup(
                    [
                        ("All", "all"),
                        ("Skills", "skill"),
                        ("Tools", "tool"),
                        ("Reports", "report"),
                        ("Memories", "memory"),
                    ],
                    "all",
                    id="lib-type",
                )
                yield Static("", classes="spacer")
                yield ChipGroup(
                    [
                        ("Active", "active"),
                        ("Proposed", "proposed"),
                        ("Retired", "retired"),
                        ("Any", "all"),
                    ],
                    "active",
                    id="lib-status",
                )
        with Horizontal():
            with Vertical(classes="sidebar wide", id="lib-side"):
                yield Static("", id="lib-count", classes="label")
                yield RowList(id="lib-list", empty=f"[{FAINT}]Nothing matches.[/]")
            with Vertical(classes="main"):
                yield Empty(
                    "[b]The Library[/b]\n\n"
                    f"[{MUTED}]What missions learned, kept for the next one:\n"
                    f"skills, tools, reports and memories. Search on the left.[/]",
                    id="lib-empty",
                )
                with Vertical(id="lib-reader"):
                    with Horizontal(classes="pane-head"):
                        yield Static("", id="lib-title", classes="pane-head-title")
                        yield Static("", classes="spacer")
                        yield Btn("Activate", "activate", variant="primary")
                        yield Btn("Retire", "retire", variant="ghost")
                        yield Btn("Copy id", "copy", variant="ghost")
                    yield Static("", id="lib-meta")
                    with VerticalScroll(id="lib-body", classes="doc"):
                        yield Markdown("", id="lib-md")

    def on_mount(self) -> None:
        self._paint_reader()

    def hints(self) -> list[tuple[str, str]]:
        return [("type", "search"), ("↓", "results"), ("click", "read")]

    def shown(self) -> None:
        if not self._loaded:
            self._loaded = True
            self._search()
        self.query_one("#lib-search", Input).focus()

    # -- search ------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "lib-search":
            return
        event.stop()
        self.query_text = event.value
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(0.25, self._search)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "lib-search":
            event.stop()
            rows = self.query_one("#lib-list", RowList)
            rows.focus()
            if rows.selectable:
                rows.activate(rows.selectable[0])

    def on_key(self, event: Any) -> None:
        if event.key == "down" and isinstance(self.screen.focused, Input):
            rows = self.query_one("#lib-list", RowList)
            if rows.selectable:
                event.stop()
                rows.focus()
                rows.activate(rows.selectable[0])

    def on_chip_group_changed(self, event: ChipGroup.Changed) -> None:
        event.stop()
        if event.group.id == "lib-type":
            self.type_filter = event.value
        else:
            self.status_filter = event.value
        self._search()

    @work(exclusive=True, group="lib-search")
    async def _search(self) -> None:
        self._searching = True
        self._search_epoch += 1
        try:
            items = await self._fetch_items()
        finally:
            self._searching = False
        self.items = self._filter(items)
        self._fingerprint = tuple(self._item_fingerprint(i) for i in self.items)
        self._body_fingerprint = None
        self._paint_list()

    async def _fetch_items(self) -> list[Any]:
        data = self.hub.data
        q = self.query_text.strip()
        self.mode = ""
        if not q or self.status_filter != "active":
            items = await self.hub.guard(lambda: data.library_search(q), [])
            self.mode = "keyword" if q else ""
        else:
            try:
                items = await data.library_hybrid_search(q)
                from rework.library.embeddings import embeddings_from_env

                self.mode = "hybrid" if embeddings_from_env() is not None else "keyword"
            except Exception:
                items = await self.hub.guard(lambda: data.library_search(q), [])
                self.mode = "keyword"
        if q and self.status_filter != "active":
            # keyword search only serves active items; widen by scanning
            everything = await self.hub.guard(lambda: data.library_search(""), [])
            needle = q.lower()
            items = [
                i
                for i in everything
                if needle in f"{i.name} {i.description} {' '.join(i.tags)}".lower()
            ]
        return list(items)

    def _filter(self, items: list[Any]) -> list[Any]:
        return [
            i
            for i in items
            if (self.type_filter == "all" or _v(i.type) == self.type_filter)
            and (self.status_filter == "all" or _v(i.status) == self.status_filter)
        ]

    def _paint_list(self) -> None:
        rows = []
        for item in self.items:
            t = _v(item.type)
            st = _v(item.status)
            colour = STATUS_COLOUR.get(st, MUTED)
            prov = item.provenance
            origin = " · ".join(
                x for x in (prov.project or "", prov.mission_id or "", prov.role or "") if x
            )
            status = "" if st == "active" else f"  [{colour}]{st}[/]"
            rows.append(
                RowData(
                    item.id,
                    f"[{ACCENT}]{TYPE_GLYPH.get(t, '·')}[/] "
                    f"[b]{esc(clip(item.name, 36))}[/b]{status}\n"
                    f"  [{MUTED}]{esc(clip(item.description or '—', 40))}[/]\n"
                    f"  [{FAINT}]{esc(t)}{' · ' + esc(origin) if origin else ''} · "
                    f"{esc(ago(item.updated_at))}[/]",
                )
            )
        lst = self.query_one("#lib-list", RowList)
        lst.set_rows(rows)
        lst.select(self.selected.id if self.selected is not None else None)
        mode = f" · {self.mode}" if self.mode else ""
        self.query_one("#lib-count", Static).update(f"{len(rows)} ITEMS{mode.upper()}")

    def _item_fingerprint(self, item: Any) -> tuple[Any, ...]:
        return (
            item.id,
            _v(item.status),
            item.version,
            item.updated_at,
            item.name,
            item.description,
            tuple(item.tags),
        )

    # -- hub ---------------------------------------------------------------

    def on_snapshot(self, snap: Snapshot) -> None:
        """The library index is shared: another instance (or a chat card) can
        add, retire or rewrite an item while this tab sits open. Re-run the
        current search when the tab is the active one and nothing the user is
        typing has changed, and re-read the open body when its version moved."""
        if self.rw.active_tab != self.TAB or not self._loaded:
            return
        self._recheck()

    @work(exclusive=True, group="lib-recheck")
    async def _recheck(self) -> None:
        if self._searching:
            return  # the user's own search is in flight; don't race it
        epoch = self._search_epoch
        items = await self.hub.guard(self._fetch_items, None)
        if items is None or self._searching or self._search_epoch != epoch:
            return  # a user search started/ran meanwhile: its results win
        items = self._filter(items)
        self.items = items
        fingerprint = tuple(self._item_fingerprint(i) for i in items)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._paint_list()
        if self.selected is not None:
            # the index row is authoritative for the reader's metadata
            fresh = next((i for i in items if i.id == self.selected.id), None)
            if fresh is not None and fresh is not self.selected:
                self.selected = fresh
                self._paint_reader()
        await self._recheck_body()

    async def _recheck_body(self) -> None:
        item = self.selected
        if item is None:
            return
        try:
            fresh, body = await self.hub.data.library_body(item.id)
        except Exception:
            return
        if fresh is None or self.selected is None or self.selected.id != item.id:
            return
        fingerprint = (item.id, int(fresh.version))
        if fingerprint == self._body_fingerprint:
            return
        self._body_fingerprint = fingerprint
        self.selected = fresh
        self._paint_reader()
        if body is not None:
            await self.query_one("#lib-md", Markdown).update(body)

    def on_row_list_activated(self, event: RowList.Activated) -> None:
        event.stop()
        item = next((i for i in self.items if i.id == event.key), None)
        if item is not None:
            self.selected = item
            self._paint_reader()
            self._load_body(item.id)

    # -- reader ------------------------------------------------------------

    def _paint_reader(self) -> None:
        item = self.selected
        self.query_one("#lib-empty").display = item is None
        self.query_one("#lib-reader").display = item is not None
        if item is None:
            return
        t = _v(item.type)
        st = _v(item.status)
        colour = STATUS_COLOUR.get(st, MUTED)
        self.query_one("#lib-title", Static).update(
            f"[{ACCENT}]{TYPE_GLYPH.get(t, '·')}[/] [b]{esc(item.name)}[/b]  [{colour}]{esc(st)}[/]"
        )
        prov = item.provenance
        meta = [
            f"[{MUTED}]{esc(t)} · v{item.version} · used {item.uses}× · updated "
            f"{esc(ago(item.updated_at))} · {esc(item.id)}[/]"
        ]
        if item.description:
            meta.insert(0, esc(item.description))
        prov_bits = [
            f"{k} {v}"
            for k, v in (
                ("origin", _v(prov.origin)),
                ("project", prov.project),
                ("mission", prov.mission_id),
                ("role", prov.role),
                ("source", prov.source_url),
            )
            if v
        ]
        if prov_bits:
            meta.append(f"[{FAINT}]{esc(' · '.join(prov_bits))}[/]")
        if item.tags:
            meta.append(" ".join(f"[{ACCENT}]#{esc(tag)}[/]" for tag in item.tags))
        self.query_one("#lib-meta", Static).update("\n".join(meta))
        for btn in self.query(Btn):
            if btn.action == "activate":
                btn.display = st != "active"
            elif btn.action == "retire":
                btn.display = st != "retired"

    @work(exclusive=True, group="lib-body")
    async def _load_body(self, item_id: str) -> None:
        md = self.query_one("#lib-md", Markdown)
        await md.update("_loading…_")
        result = await self.hub.guard(lambda: self.hub.data.library_body(item_id), (None, None))
        _item, body = result
        if self.selected is None or self.selected.id != item_id:
            return
        self._body_fingerprint = (_item.id, int(_item.version)) if _item is not None else None
        if body is None:
            body = (
                "_The body is not on this machine._\n\nThe library git repo lives with the "
                "daemon; this device shares only the index. Mirror the repo to read bodies here."
            )
        await md.update(body)

    def on_btn_pressed(self, event: Btn.Pressed) -> None:
        event.stop()
        if event.action == "new":
            self.new_item()
            return
        item = self.selected
        if item is None:
            return
        if event.action == "copy":
            self.rw.copy_to_clipboard(item.id)
            self.toast(f"Copied {item.id}")
        elif event.action in ("activate", "retire"):
            self._set_status(item.id, event.action == "retire")

    @work(group="lib-write")
    async def _set_status(self, item_id: str, retire: bool) -> None:
        try:
            ok = await self.hub.data.library_set_status(item_id, retire)
        except Exception as exc:
            self.toast(f"Could not update: {exc}", severity="error")
            return
        if ok:
            self.toast("Retired" if retire else "Activated")
            index, _ = await self.hub.data.library()
            self.selected = await index.get_item(item_id)
            self._paint_reader()
            self._search()

    def new_item(self) -> None:
        def done(values: dict[str, str] | None) -> None:
            if values and values.get("name", "").strip() and values.get("body", "").strip():
                self.run_worker(self._add(values), group="lib-write")
            elif values is not None:
                self.toast("Name and body are required", severity="warning")

        self.rw.push_screen(
            Form(
                "New library item",
                [
                    Field(
                        "type",
                        "Type",
                        kind="choice",
                        value="skill",
                        choices=[
                            ("skill", "skill"),
                            ("memory", "memory"),
                            ("report", "report"),
                            ("tool", "tool"),
                        ],
                    ),
                    Field("name", "Name", placeholder="e.g. pytest-flaky-triage"),
                    Field("description", "One-line description"),
                    Field("tags", "Tags (comma separated)"),
                    Field("body", "Body (markdown)", kind="area"),
                ],
                submit="Add",
            ),
            done,
        )

    async def _add(self, values: dict[str, str]) -> None:
        try:
            item, note = await self.hub.data.library_add(
                type_=values.get("type") or "skill",
                name=values["name"].strip(),
                description=values.get("description", "").strip(),
                body=values["body"],
                tags=[t.strip() for t in values.get("tags", "").split(",") if t.strip()],
            )
        except Exception as exc:
            self.toast(f"Could not add: {clip(str(exc), 200)}", severity="error")
            return
        self.toast(note or f"Added {item.name}", title="Library")
        self.selected = item
        self._paint_reader()
        self._load_body(item.id)
        self._search()
