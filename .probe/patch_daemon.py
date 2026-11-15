from pathlib import Path

p = Path("src/rework/orchestrator/daemon.py")
s = p.read_text()

old = '''        log.error("runner for %s crashed: %s: %s", mission_id, type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        try:
            mission = await self._store.get_mission(mission_id)
            if mission is not None and mission.status not in (
                MissionStatus.DONE,
                MissionStatus.FAILED,
                MissionStatus.CANCELLED,
            ):
                await self._store.update_mission(
                    mission_id,
                    status=MissionStatus.FAILED,
                    result={**(mission.result or {}), "error": error[:2000]},
                )
'''
new = '''        log.error("runner for %s crashed: %s: %s", mission_id, type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        # Delivery errors (push / gh) are kept WHOLE: git's useful line is the
        # LAST one, after the `remote:` banner and the forge's hints, so a
        # 2000-char prefix ends before the reason. Every other error stays
        # trimmed as before (a runaway traceback is not a mission report).
        from rework.nodes.gh import GHError
        from rework.nodes.git_ops import PushError

        keep_whole = isinstance(exc, (PushError, GHError))
        try:
            mission = await self._store.get_mission(mission_id)
            if mission is not None and mission.status not in (
                MissionStatus.DONE,
                MissionStatus.FAILED,
                MissionStatus.CANCELLED,
            ):
                await self._store.update_mission(
                    mission_id,
                    status=MissionStatus.FAILED,
                    result={
                        **(mission.result or {}),
                        "error": error if keep_whole else error[:2000],
                    },
                )
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("ok")
