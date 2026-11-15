from pathlib import Path

# 1. TuiData.retry_pr — the TUI's door to the SAME helper the CLI calls.
p = Path("src/rework/tui/data.py")
s = p.read_text()
anchor = '''    async def prioritize_mission(self, mission_id: str, priority: str = "high") -> Any:
'''
assert anchor in s
s = s.replace(
    anchor,
    '''    async def retry_pr(self, mission_id: str) -> tuple[bool, str]:
        """Retry push+PR for a finished mission whose delivery failed. Shares
        `rework.orchestrator.delivery.retry_pr` with `rework mission retry-pr`,
        so the button and the command can never disagree."""
        from rework.kernel.events.bus import InProcessEventBus
        from rework.orchestrator.delivery import retry_pr

        store = await self.store()
        return await retry_pr(store, InProcessEventBus(store), mission_id)

''' + anchor,
    1,
)
p.write_text(s)

# 2. actions.py: a "retry-pr" verb gated on the finished-but-PR-pending state.
p = Path("src/rework/tui/actions.py")
s = p.read_text()
old_verbs = '''VERBS = {
    "pause": ("pause_mission", "Paused", "Pausing stops the roles; everything stays resumable."),
    "resume": ("resume_mission", "Resumed", ""),
    "cancel": ("cancel_mission", "Cancelled", ""),
}
'''
new_verbs = '''VERBS = {
    "pause": ("pause_mission", "Paused", "Pausing stops the roles; everything stays resumable."),
    "resume": ("resume_mission", "Resumed", ""),
    "cancel": ("cancel_mission", "Cancelled", ""),
}

# "Finished, but the PR was never delivered": the state the delivery helper
# writes into mission.result when a push/`gh pr create` fails for good.
PENDING_KEY = "pr_pending"


def delivery_pending(result: dict[str, Any] | None) -> bool:
    return bool((result or {}).get(PENDING_KEY))
'''
assert old_verbs in s
s = s.replace(old_verbs, new_verbs, 1)

old_control = '''async def control(app: ReworkApp, mission_id: str, verb: str) -> bool:
    if verb == "fetch":
        return await fetch(app, mission_id)
'''
new_control = '''async def control(app: ReworkApp, mission_id: str, verb: str) -> bool:
    if verb == "fetch":
        return await fetch(app, mission_id)
    if verb == "retry-pr":
        return await retry_pr(app, mission_id)
'''
assert old_control in s
s = s.replace(old_control, new_control, 1)

old_fetch = '''async def fetch(app: ReworkApp, mission_id: str) -> bool:
'''
new_fetch = '''async def retry_pr(app: ReworkApp, mission_id: str) -> bool:
    """Re-deliver a finished mission's PR. Same helper as
    `rework mission retry-pr`; the full error is shown, never a clipped one."""
    app.notify(f"Retrying PR for {mission_id}…", timeout=3)
    try:
        ok, detail = await app.hub.data.retry_pr(mission_id)
    except Exception as exc:
        app.notify(f"Retry PR failed: {exc}", severity="error", timeout=10)
        return False
    if not ok:
        app.notify(f"PR still not delivered: {detail}", severity="error", timeout=12)
        await app.hub.refresh()
        return False
    app.notify(f"PR opened: {detail}", title="Retry PR", timeout=8)
    await app.hub.refresh()
    return True


async def fetch(app: ReworkApp, mission_id: str) -> bool:
'''
assert old_fetch in s
s = s.replace(old_fetch, new_fetch, 1)
p.write_text(s)

# 3. cards.py: the button, shown exactly when the mission is finished-but-pending.
p = Path("src/rework/tui/widgets/cards.py")
s = p.read_text()
old_btn = '''            yield Btn("Preview", "preview", classes="mc-previewbtn")
'''
new_btn = '''            yield Btn("Preview", "preview", classes="mc-previewbtn")
            yield Btn("Retry PR", "retry-pr", variant="primary", classes="mc-retrypr")
'''
assert old_btn in s
s = s.replace(old_btn, new_btn, 1)

old_paint = '''        for btn in self.query(".mc-previewbtn").results(Btn):
            btn.display = bool(pr_url)
'''
new_paint = '''        for btn in self.query(".mc-previewbtn").results(Btn):
            btn.display = bool(pr_url)
        # finished, work kept, PR not delivered: the one button that fixes it
        pending = delivery_pending(getattr(m, "result", None) or {})
        for btn in self.query(".mc-retrypr").results(Btn):
            btn.display = pending and not pr_url
'''
assert old_paint in s
s = s.replace(old_paint, new_paint, 1)
s = s.replace(
    '''from rework.tui.actions import can
''',
    '''from rework.tui.actions import can, delivery_pending
''',
    1,
)
p.write_text(s)
print("ok")
