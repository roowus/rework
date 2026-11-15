# rework TUI: design language

The TUI is the owner's cockpit for rework. It should feel like a well-made
desktop app that happens to live in a terminal: quiet, dense, fast, and
predictable. Claude Code / pi for the conversation, a real window for everything
else.

**Minimal, but put together.** One accent colour, used sparingly. Hierarchy comes
from tone (background steps) and typography (bold / muted), not from boxes. If
something looks busy, remove a line before adding a colour.

---

## 1. Tokens

All colours live in `src/rework/tui/theme.py` and are exposed to TCSS as
variables. Never hard-code a hex value in a widget.

| token | value | use |
|---|---|---|
| `$bg` | `#0c0c10` | app background |
| `$surface` | `#121218` | panes, sidebars |
| `$raised` | `#18181f` | cards, hovered rows, inputs |
| `$overlay` | `#1e1e27` | menus, modals, selected row |
| `$line` | `#24242d` | separators (1 cell, never doubled) |
| `$line-strong` | `#34343f` | focused input border |
| `$text` | `#e6e6eb` | primary text |
| `$muted` | `#8c8c99` | secondary text, labels |
| `$faint` | `#5a5a66` | tertiary text, disabled, hints |
| `$accent` | `#a394f7` | THE accent: focus, selection bar, live state, primary button |
| `$accent-dim` | `#3a3360` | accent backgrounds (selected chip, active tab underline fill) |
| `$ok` | `#76c893` | done, approved, success |
| `$warn` | `#e3b66b` | needs you: awaiting gate, on hold, unread |
| `$danger` | `#ec6a6a` | failed, reject, destructive |

Rules:
- `$accent` means **"this is live / this is where you are"**. Not decoration.
- `$warn` means **"a human is needed"**. The gates badge, awaiting-gate missions,
  gate cards. Nothing else is amber.
- Red is only for failure and destructive buttons.

## 2. Typography

Terminals give us three weights; use them deliberately.

- **Section label**: UPPERCASE, `$faint`, bold, e.g. `CHATS`, `RUNS`. One per region.
- **Title**: `$text` bold. Mission objective, chat title, gate question.
- **Body**: `$text`.
- **Meta**: `$muted`. ids, ages, costs, model names. Joined with ` · `.
- **Hint**: `$faint`. Key hints, empty-state help.
- Numbers right-aligned in columns; money as `$0.042` (3 decimals under $1,
  2 above), tokens as `12.4k` / `1.2M`, durations as `4m 12s`, ages as `3m ago`.

## 3. Status vocabulary

One glyph + one colour per state, everywhere (lists, flow strips, graph nodes,
badges). Defined once in `theme.py::STATUS`.

| state | glyph | colour |
|---|---|---|
| pending / intent | `○` | `$faint` |
| running | `●` | `$accent` |
| awaiting gate / halted | `◆` | `$warn` |
| on hold (paused) | `‖` | `$warn` |
| done / completed / approved | `✓` | `$ok` |
| failed / rejected | `✗` | `$danger` |
| cancelled / aborted | `⊘` | `$faint` |

Trajectory glyphs (Claude-Code familiar):

```
› you typed this                         (user; $muted glyph, $text body)
⏺ orchestrator prose                    (assistant; $text)
⏺ read_file  path=src/app.py            (tool call; name bold, args $muted)
  ⎿ 112 lines · def add(a, b): …        (tool result; $muted, red when error)
  turn 3 · 3.8k tok                      (turn footer; $faint)
```

## 3b. Two levels in the Graph

The Graph draws two levels and they must never be confused:

- **Inside one run**: the role's own LangGraph node names, shown as a strip
  prefixed `inside this run:` in `$faint`, with TUI-only friendly names
  (`prepare` -> `set up`, `loop` -> `working (tool calls)`, `report` ->
  `report`, `handoff` -> `hand off`; unknown names pass through unchanged).
  Only the current step is `▶` + `$accent` bold.
- **The mission**: review rounds, coder(s) -> audit -> revision. The edge into
  an audit carries its findings (`request changes: 2 high`); the edge leaving
  an audit carries the audit's mechanical verdict (`request changes`,
  `approve`) and nothing more - which direction it means is implied by where
  the arrow points, toward a revision or on to the gates/end (a mission capped
  by `max_rounds` ends on `request changes` too, and the arrow says so). The
  leaving edge stays unlabelled while the round's audit is still running: there
  is no verdict yet, and the into-audit edge already carries the state. An edge
  is `│ ▼ label`, so its height is auto: a fixed height clipped the label.

## 4. Layout

```
 ◆ rework   File  Edit  View  Mission  Help                 ● live · ◆ 2 · $1.24 today
 Work   Dashboard   Gates 2   Library   Cost
──────────────────────────────────────────────────────────────────────────────────────
 <active tab, full width>
──────────────────────────────────────────────────────────────────────────────────────
 enter send · shift+enter newline · alt+1-5 tabs · esc esc quit
```

- **Menu bar** (row 1): wordmark, dropdown menus, and the global status cluster
  on the right: connection (`● live` / `○ offline`), open gates (`◆ n`, amber,
  clickable → Gates), spend today.
- **Tab bar** (row 2): text tabs; the active one is `$text` bold with an
  `$accent` underline; others `$muted`. Gates shows an amber count.
- **Hint bar** (last row): context-specific key hints in `$faint`. Never more
  than one line.
- Inside a tab: a **sidebar** (`$surface`, fixed width ~30) and a **main area**
  (`$bg`), divided by one `$line` column. No outer borders.

## 5. Components

Each lives in `src/rework/tui/widgets/` and is the only way that thing is drawn.

| component | what it is |
|---|---|
| `MenuBar`, `Menu` | top bar + dropdown overlay; click or alt-key to open |
| `TabBar` | the section switcher |
| `HintBar` | bottom key hints, set per view |
| `Row` (+ `RowList`) | a clickable list row: optional glyph, title, meta, right-aligned value. **One click selects AND acts.** Selected row: `$overlay` bg + 1-cell `$accent` bar on the left |
| `Card` | `$raised` block, optional left bar colour (state), title / meta / body / actions |
| `Chip` | small toggle: `$raised` bg, selected = `$accent-dim` bg + `$text` |
| `Btn` | flat button: default `$raised`; `primary` = `$accent` text on `$accent-dim`; `danger` = `$danger` text. One line tall, no border |
| `Breadcrumb` | `Dashboard › polystats › m-1a2b › coder #2`, each segment clickable |
| `Trajectory` | renders turn / tool_call / tool_result steps with the glyphs above; long results fold to one line, click to expand |
| `FlowStrip` | a mission's steps as `investigate ✓ ─ code ● ─ audit ○ ─ ship ◆` |
| `GraphView` | a LangGraph drawn in boxes + arrows, current node highlighted |
| `Prompt` | multi-line editor: enter sends, shift+enter newline, large pastes become attachments |
| `Picker` | modal list with a filter box (models, projects, effort) |
| `Empty` | centred `$faint` explanation + one primary action |
| `Stat` | big number + label (cost tab, dashboard header) |
| `Heatmap` | calendar grid, 5 shades from `$raised` to `$accent` |

## 6. Interaction rules

1. **Mouse first, keys as accelerators.** Every action has a visible clickable
   control. A key binding never exists without a button or menu item.
2. **One click acts.** Clicking a row opens it. No double-click semantics.
3. **Enter = deeper, Esc = back.** Esc on a top-level view does nothing;
   **Esc twice within 1s quits** (with a toast on the first press).
4. **Ctrl+C copies** (selected text, or the focused row's id). It never quits.
5. **Shortcuts use modifiers** (`alt+1..5` tabs, `ctrl+n` new chat,
   `ctrl+r` refresh, `ctrl+b` sidebar, `ctrl+p` palette) so they work while
   typing. Single-letter keys only in views without a text box.
6. **Nothing auto-opens.** Bare `rework` lands on Work with no chat selected.
7. **Destructive actions confirm inline** (the button turns into
   `Really cancel? [Yes] [No]`), never with a surprise modal.
8. **Never lose input.** A failed send keeps the prompt text.

## 7. Liveness & failure

- One `Hub` polls the store (missions, gates, projects, today's spend) on an
  interval and broadcasts a snapshot; views subscribe. Views that need more
  (an open mission's events) poll through the Hub's guarded helper.
- Every poll is wrapped: a failure becomes `○ offline` in the menu bar and a
  stale snapshot, never an exception. `ReworkApp._handle_exception` turns
  anything that escapes into a toast.
- Long work (model calls, fetch, library index) runs in workers; the UI never
  blocks.
- Motion: the running glyph pulses; nothing else animates.

## 8. Notifications

When a new gate opens: in-app toast, terminal bell, and/or a macOS notification.
`REWORK_NOTIFY` is a comma list of `toast,bell,os` (or `none`); default
`toast,bell`. Also settable from File › Settings (saved to
`~/.rework/tui.json`).
