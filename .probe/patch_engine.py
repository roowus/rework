from pathlib import Path

p = Path("src/rework/orchestrator/engine.py")
s = p.read_text()

old = '''        await push_branch(worktree, branch)
        url = await pr_create(worktree, head=branch, title=title, body=body)
        await self._bus.publish(
            EventRecord(
                mission_id=mission.id,
                type=EventType.INFO,
                payload={"note": f"PR opened: {url}"},
            )
        )
        return url
'''
new = '''        # Push + PR through the shared delivery helper: a transient remote
        # failure is RETRIED there, and a persistent one leaves the mission
        # retryable instead of raising (which used to fail a finished mission).
        url = await deliver_pr(
            self._store,
            self._bus,
            mission.id,
            branch=branch,
            worktree=worktree,
            title=title,
            body=body,
        )
        return url
'''
assert old in s
s = s.replace(old, new)

old_imports = '''    async def _create_pr(self, mission: Mission, results: dict[str, Run]) -> str | None:
        """Push the coder's branch and open a PR. Mechanical: title/body from
        the brief + audit verdict; the human does the merging."""
        from rework.nodes.gh import pr_create
        from rework.nodes.git_ops import push_branch
'''
new_imports = '''    async def _create_pr(self, mission: Mission, results: dict[str, Run]) -> str | None:
        """Push the coder's branch and open a PR. Mechanical: title/body from
        the brief + audit verdict; the human does the merging."""
        from rework.orchestrator.delivery import deliver_pr
'''
assert old_imports in s
s = s.replace(old_imports, new_imports)

# _synthesize must carry the delivery record across (it rebuilds `result`).
old_synth = '''        result: dict[str, Any] = {"artifacts": artifacts}
        if summary:
            result["summary"] = summary
'''
new_synth = '''        result: dict[str, Any] = {"artifacts": artifacts}
        # the PR delivery record (pr_url / pr_pending / pr_error / what a retry
        # needs) is written by _create_pr BEFORE this rebuild: carry it over, or
        # "finished but PR pending" would be forgotten the moment we go DONE.
        current_mission = await self._store.get_mission(mission_id)
        for key in DELIVERY_RESULT_KEYS:
            value = ((current_mission.result if current_mission else None) or {}).get(key)
            if value is not None:
                result[key] = value
        if summary:
            result["summary"] = summary
'''
assert old_synth in s
s = s.replace(old_synth, new_synth)

# import the key tuple at module level with the other imports
anchor = "from rework.kernel.state.records import "
idx = s.index(anchor)
line_end = s.index("\n)\n", idx)
s = s[: line_end + len("\n)\n")] + "from rework.orchestrator.delivery import DELIVERY_RESULT_KEYS\n" + s[line_end + len("\n)\n") :]
p.write_text(s)
print("ok")
