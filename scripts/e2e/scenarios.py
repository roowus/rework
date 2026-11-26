"""Live TUI scenarios for tui_live.py. Each one drives REAL TUI apps against a
REAL daemon and records findings instead of failing fast, so one run reports
every problem it can see."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tui_live import World, scenario, shot, until

from rework.kernel.llm.types import ChatResult, ToolCall, Usage

# canned role artifacts (schema-valid), kept as data next to this file
_FIXTURES = Path(__file__).parent / "fixtures"
AUDIT_CLEAN = (_FIXTURES / "audit_clean.json").read_text()
INV_REPORT = (_FIXTURES / "inv_report.json").read_text()


class ScriptedChat:
    """A provider the TUI's chat engine talks to: canned turns, in order."""

    name = "scripted"

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = list(turns)

    async def complete(self, request: Any) -> ChatResult:
        if not self.turns:
            return ChatResult(text="(no more script)", usage=Usage(input_tokens=1, output_tokens=1))
        return self.turns.pop(0)


def propose(objective: str, title: str, plan: str = "echo") -> ChatResult:
    return ChatResult(
        text="Here's what I'd run.",
        tool_calls=[
            ToolCall(
                id=f"p-{title}",
                name="propose_mission",
                arguments={"objective": objective, "plan": plan, "title": title},
            )
        ],
        usage=Usage(input_tokens=900, output_tokens=60),
    )


def install_chat(turns: list[ChatResult]) -> None:
    import rework.kernel.llm.adapters as adapters

    provider = ScriptedChat(turns)
    adapters.build_provider = lambda entry, extra=None: provider  # type: ignore[assignment]


async def type_text(pilot: Any, text: str) -> None:
    for ch in text:
        await pilot.press(ch if ch != " " else "space")


def statuses(app: Any) -> dict[str, str]:
    return {m.id: m.status.value for m in app.hub.snapshot.missions}


@scenario
async def chat_launch_and_cross_tui_gate(world: World) -> None:
    """Owner A chats, proposes and launches; owner B (second TUI) approves the
    gate; both screens must converge on DONE without a restart."""
    from rework.tui.app import ReworkApp
    from rework.tui.widgets.cards import MissionCard, ProposalCard

    # the daemon's orchestrator: spawn echo, wait for it, then finish
    world.script(
        {
            "MISSION ORCHESTRATOR": [
                {
                    "tool_calls": [
                        {
                            "name": "spawn_role",
                            "arguments": {"role": "echo", "step": "echo", "brief": "say hello"},
                        },
                        {"name": "wait", "arguments": {}},
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "name": "finish",
                            "arguments": {
                                "outcome": "done",
                                "summary": "Echo ran and the owner approved the greeting gate.",
                            },
                        }
                    ]
                },
            ]
        }
    )
    world.start_daemon()
    install_chat([propose("say hello", "Greeting")])
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        await pb.pause(0.8)
        await shot(a, world.out, "01-a-start")

        await type_text(pa, "say hello")
        await pa.press("enter")
        if not await until(pa, lambda: len(a.query(ProposalCard)) >= 1):
            world.finding("work/chat", "no proposal card after a proposing turn")
            await shot(a, world.out, "02-a-no-card")
            return
        await shot(a, world.out, "02-a-proposal")
        card = a.query(ProposalCard).first()
        launch = next(btn for btn in card.query("Btn") if getattr(btn, "action", "") == "launch")
        await until(pa, lambda: launch.region.width > 0)
        await pa.click(launch)
        if not await until(pa, lambda: len(a.query(MissionCard)) >= 1, 10):
            world.finding("work/launch", "launch produced no mission card")
        mid = next(iter(statuses(a)), None) if await until(pa, lambda: statuses(a), 10) else None
        await shot(a, world.out, "03-a-launched")
        if mid is None:
            world.finding("work/launch", "the launched mission never appeared in A's snapshot")
            return

        # B: the mission and its gate must appear without any action on B
        if not await until(pb, lambda: mid in statuses(b), 15):
            world.finding("sync", "TUI B never saw the mission A launched", mission=mid)
        if not await until(pb, lambda: statuses(b).get(mid) == "awaiting_gate", 20):
            world.finding(
                "sync", "B did not show awaiting_gate", seen=statuses(b).get(mid), mission=mid
            )
        await pb.press("alt+3")
        await pb.pause(1.0)
        await shot(b, world.out, "04-b-gates")
        gates = await b.hub.data.open_gates()
        if not gates:
            world.finding("gates", "B's Gates data has no open gate", mission=mid)
            return
        ok = await b.hub.data.decide_gate(gates[0].id, True)
        if not ok:
            world.finding("gates", "approve from B returned False")

        # both converge on done
        for label, app, pilot in (("A", a, pa), ("B", b, pb)):
            if not await until(pilot, lambda app=app: statuses(app).get(mid) == "done", 25):
                world.finding(
                    "sync",
                    f"{label} did not converge on done",
                    seen=statuses(app).get(mid),
                    mission=mid,
                )
        await shot(a, world.out, "05-a-done")
        await shot(b, world.out, "06-b-done")
        # the Gates tab on A must not still list the decided gate
        await pa.press("alt+3")
        await pa.pause(1.0)
        if await a.hub.data.open_gates():
            world.finding("gates", "A still lists an open gate after B approved")
        await shot(a, world.out, "07-a-gates-after")
        for label, app in (("A", a), ("B", b)):
            if app.swallowed:
                world.finding(
                    "errors",
                    f"{label} swallowed exceptions",
                    errors=[repr(e) for e in app.swallowed],
                )


@scenario
async def finished_card_controls(world: World) -> None:
    """After DONE the mission card must offer no Pause/Cancel (both are illegal
    on a finished mission and the daemon refuses them)."""
    from rework.tui.app import ReworkApp
    from rework.tui.widgets.cards import MissionCard, ProposalCard

    # a REPO-LESS draft mission that really runs a role (investigator) to done
    world.script(
        {
            "MISSION ORCHESTRATOR": [
                {
                    "tool_calls": [
                        {
                            "name": "spawn_role",
                            "arguments": {
                                "role": "investigator",
                                "step": "research",
                                "brief": "say hi",
                            },
                        },
                        {"name": "wait", "arguments": {}},
                    ]
                },
                # draft audits its claims: the finish rail requires an auditor
                {
                    "tool_calls": [
                        {
                            "name": "spawn_role",
                            "arguments": {
                                "role": "auditor",
                                "step": "audit",
                                "brief": "audit the report",
                            },
                        },
                        {"name": "wait", "arguments": {}},
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "name": "finish",
                            "arguments": {
                                "outcome": "done",
                                "summary": "Researched the greeting request and wrote the report.",
                            },
                        }
                    ]
                },
            ],
            "Find issues before they reach production": [
                {"text": "The report's claims are consistent."},
                {"text": AUDIT_CLEAN},
            ],
            "Investigate any brief": [
                {"text": "Nothing to read: this is a greeting."},
                {"text": INV_REPORT},
            ],
        }
    )
    world.start_daemon()
    install_chat([propose("say hi", "Hi", plan="draft")])
    a = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa:
        await pa.pause(0.8)
        await type_text(pa, "say hi")
        await pa.press("enter")
        await until(pa, lambda: len(a.query(ProposalCard)) >= 1)
        card = a.query(ProposalCard).first()
        launch = next(b for b in card.query("Btn") if getattr(b, "action", "") == "launch")
        await until(pa, lambda: launch.region.width > 0)
        await pa.click(launch)
        if not await until(pa, lambda: len(a.query(MissionCard)) >= 1, 15):
            await shot(a, world.out, "no-mission-card")
            world.finding(
                "work/launch",
                "a 'draft' proposal produced no mission card",
                missions=statuses(a),
                swallowed=[repr(e) for e in a.swallowed],
                toasts=[str(n.message)[:200] for n in getattr(a, "_notifications", [])],
            )
            return
        mc = a.query(MissionCard).first()
        if not await until(pa, lambda: mc.status == "done", 30):
            world.finding("card", "card never reached done", card_status=mc.status)
        await pa.pause(2.0)
        shown = sorted(
            b.action
            for b in mc.query("Btn")
            if b.display and b.action in ("pause", "cancel", "resume")
        )
        if shown:
            world.finding(
                "card",
                "finished card still offers owner controls",
                shown=shown,
                card_status=mc.status,
                snap_status=statuses(a).get(mc.mission_id),
            )
        await shot(a, world.out, "finished-card")
