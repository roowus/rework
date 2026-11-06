# ADR 0006: The TUI is rebuilt on a design system, a single hub, and a store-only contract

## Status

Accepted (2026-11-05)

## Context

The first TUI grew screen by screen: inconsistent layouts, flows that did not
connect (a proposal in one place, its mission in another), bugs from Textual
edge cases (DataTable double-click, swallowed keys, a poll exception taking the
app down). The owner asked for a rebuild that feels like Claude Code / pi for
the conversation and like a real desktop window for everything else, with one
consistent design language.

## Decision

1. **Design system first.** `docs/tui/DESIGN.md` defines tokens (one accent,
   amber only for "a human is needed"), a status-glyph vocabulary, components
   and interaction rules. `tui/theme.py` is the only place colours and formats
   live; `tui/widgets/` is the only way a component is drawn.
2. **One hub.** `tui/hub.py` polls the cheap global state on an interval and
   broadcasts a snapshot to every view. Views poll details through
   `Hub.guard`, which degrades to "offline", never to an exception.
3. **Views own their sections; actions are shared.** `tui/views/*` one module
   per tab; `tui/actions.py` holds every owner action (decide gate, pause,
   cancel, fetch) so the same button behaves the same everywhere.
4. **Conversation logic is UI-free.** `tui/conversation.py` builds the
   orchestrator context, replays stored chats into renderable items and turns a
   confirmed proposal into a mission; it is testable without widgets.
5. **Store-only.** The TUI never decides what the daemon owns. Everything it
   reads or writes is a store row; features that need engine support are listed
   in `docs/tui/BACKEND-NEEDS.md` rather than faked in the client.
6. **Custom list rows instead of DataTable.** `RowList` / `Row` give one-click
   activation, in-place updates without flicker, and section headers.

## Consequences

- New surfaces compose from the kit, so they look native by construction.
- Tests drive the real app with Pilot and assert writes against the store
  (`tests/tui/`); `tests/tui/shots.py` renders screenshots for design review.
- Engine features (an LLM mission orchestrator with its own model, role graph
  topology events, richer gate kinds, compute-time accounting) surface in the
  UI as soon as the backend emits them; until then the UI shows what exists.
