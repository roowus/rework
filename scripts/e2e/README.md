# Live TUI end-to-end runs

`tui_live.py` starts a REAL `rework serve` on a throwaway database and a free
port, opens one or two REAL TUI apps (Textual pilot) against it, runs a
scenario from `scenarios.py`, and writes screenshots plus `findings.json`.

    uv run python scripts/e2e/tui_live.py <scenario> /tmp/rwtui

The daemon's model turns come from `REWORK_FAKE_SCRIPT` (set per scenario with
`world.script({...})`): routes keyed by a substring of the system prompt
("MISSION ORCHESTRATOR", a role's goal line), consumed in order. No model is
called and nothing touches `~/.rework`. Exit code 1 means findings.

Scenarios:

- `chat_launch_and_cross_tui_gate`: TUI A chats, proposes and launches an echo
  mission; TUI B approves the gate; both must converge on done.
- `finished_card_controls`: a repo-less draft mission runs an investigator and
  an auditor to done; the finished card must offer no pause/cancel.
- `dashboard_controls_two_tuis`: pause, resume and cancel from A's Dashboard;
  B, the daemon and A's buttons must agree after every step, and a cancelled
  mission must leave no open gate.
- `gates_tab_answer_and_badge`: an orchestrator question answered by typing in
  B's Gates tab reaches the orchestrator; the badge clears in both TUIs.
- `same_chat_turns_on_the_daemon`: both TUIs on one chat, every turn run by the daemon (the
  TUIs' model factory is booby-trapped).
- `live_updates_are_fast`: a TUI polling every 30 s sees another TUI's pause within
  about a second (the daemon's event stream).
- `heartbeat_from_the_daemon`: TUI B's store reads for its heartbeat are booby-trapped; its
  missions, gate badge and spinner must all arrive through `/v1/snapshot`.
- `tour_every_tab_and_modal`: every tab, Dashboard detail tab and menu form opens cleanly.
