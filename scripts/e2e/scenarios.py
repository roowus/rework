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


def _orchestrator_parks_on_owner(question: str) -> dict[str, Any]:
    """A daemon script whose orchestrator asks the owner and then waits, so the
    mission stays live long enough to be paused, resumed and cancelled."""
    return {
        "MISSION ORCHESTRATOR": [
            {"tool_calls": [{"name": "ask_owner", "arguments": {"question": question}}]},
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "outcome": "done",
                            "summary": "The owner answered; nothing else was needed.",
                        },
                    }
                ]
            },
        ]
    }


async def _launch_one(world: World, a: Any, pa: Any, objective: str) -> str | None:
    from rework.tui.widgets.cards import MissionCard, ProposalCard

    await type_text(pa, objective)
    await pa.press("enter")
    if not await until(pa, lambda: len(a.query(ProposalCard)) >= 1):
        world.finding("work/chat", "no proposal card", objective=objective)
        return None
    card = list(a.query(ProposalCard))[-1]
    launch = next(b for b in card.query("Btn") if getattr(b, "action", "") == "launch")
    await until(pa, lambda: launch.region.width > 0)
    await pa.click(launch)
    if not await until(pa, lambda: len(a.query(MissionCard)) >= 1, 15):
        world.finding("work/launch", "no mission card after launch", objective=objective)
        return None
    return str(list(a.query(MissionCard))[-1].mission_id)


@scenario
async def dashboard_controls_two_tuis(world: World) -> None:
    """Pause, resume and cancel from TUI A's Dashboard; TUI B, the daemon and
    A's own buttons must agree after every step."""
    from rework.tui.app import ReworkApp

    world.script(_orchestrator_parks_on_owner("Which greeting do you prefer?"))
    world.start_daemon()
    install_chat([propose("pick a greeting", "Greeting")])
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        mid = await _launch_one(world, a, pa, "pick a greeting")
        if mid is None:
            return
        if not await until(pa, lambda: statuses(a).get(mid) == "awaiting_gate", 20):
            world.finding("sync", "mission never reached awaiting_gate", seen=statuses(a).get(mid))
        await pa.press("alt+2")
        a.view("dashboard").open_mission(mid)
        await pa.pause(1.0)
        await shot(a, world.out, "dash-01-open")

        dash = a.view("dashboard")

        def button(verb: str) -> Any:
            return next(
                (x for x in dash.query("Btn") if getattr(x, "action", "") == verb and x.display),
                None,
            )

        async def press(verb: str, *, twice: bool = False) -> bool:
            btn = button(verb)
            if btn is None:
                world.finding("dashboard", f"no visible {verb} button", status=statuses(a).get(mid))
                return False
            await until(pa, lambda: btn.region.width > 0)
            await pa.click(btn)
            if twice:  # cancel is armed by the first click
                await pa.pause(0.2)
                await pa.click(btn)
            return True

        for verb, want, twice in (
            ("pause", "on_hold", False),
            ("resume", "awaiting_gate", False),
            ("cancel", "cancelled", True),
        ):
            if not await press(verb, twice=twice):
                continue
            for label, app, pilot in (("A", a, pa), ("B", b, pb)):
                if not await until(
                    pilot, lambda app=app, want=want: statuses(app).get(mid) == want, 15
                ):
                    world.finding(
                        "sync", f"after {verb}, {label} shows {statuses(app).get(mid)} not {want}"
                    )
            await shot(a, world.out, f"dash-{verb}")
        # a cancelled mission offers none of the verbs
        await pa.pause(1.0)
        left = sorted(v for v in ("pause", "resume", "cancel") if button(v) is not None)
        if left:
            world.finding("dashboard", "cancelled mission still offers controls", shown=left)
        # and its open owner question is gone from both Gates lists
        for label, app in (("A", a), ("B", b)):
            open_for = [g for g in await app.hub.data.open_gates() if g.mission_id == mid]
            if open_for:
                world.finding("gates", f"{label}: a cancelled mission still has an open gate")
        for label, app in (("A", a), ("B", b)):
            if app.swallowed:
                world.finding(
                    "errors", f"{label} swallowed", errors=[repr(e) for e in app.swallowed]
                )


@scenario
async def gates_tab_answer_and_badge(world: World) -> None:
    """An orchestrator question answered from TUI B's Gates tab by typing: the
    answer reaches the orchestrator, the badge clears in BOTH TUIs, and the
    Decided list shows it."""
    from textual.widgets import TextArea

    from rework.tui.app import ReworkApp

    world.script(
        {
            "MISSION ORCHESTRATOR": [
                {
                    "tool_calls": [
                        {
                            "name": "ask_owner",
                            "arguments": {"question": "Formal or casual greeting?"},
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "name": "finish",
                            "arguments": {
                                "outcome": "done",
                                "summary": "The owner chose the greeting style.",
                            },
                        }
                    ]
                },
            ]
        }
    )
    world.start_daemon()
    install_chat([propose("greet formally or casually", "Greeting")])
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        mid = await _launch_one(world, a, pa, "greet formally or casually")
        if mid is None:
            return

        def badge(app: Any) -> int:
            return len([g for g in app.hub.snapshot.gates if g.status.value == "open"])

        for label, app, pilot in (("A", a, pa), ("B", b, pb)):
            if not await until(pilot, lambda app=app: badge(app) == 1, 20):
                world.finding("gates", f"{label}: badge never showed the question", n=badge(app))
        await pb.press("alt+3")
        await pb.pause(1.0)
        gates_view = b.view("gates")
        if gates_view.selected is None:
            world.finding("gates", "B: the open question is not selected on entry")
            gates = await b.hub.data.open_gates()
            if not gates:
                return
        area = b.query_one("#gate-text", TextArea)
        area.focus()
        area.text = "casual"
        await shot(b, world.out, "gates-01-typed")
        await pb.press("alt+a")
        await pb.pause(0.5)
        for label, app, pilot in (("A", a, pa), ("B", b, pb)):
            if not await until(pilot, lambda app=app: badge(app) == 0, 15):
                world.finding("gates", f"{label}: badge still shows an open gate", n=badge(app))
        if not await until(pa, lambda: statuses(a).get(mid) == "done", 25):
            world.finding(
                "sync", "mission did not finish after the answer", seen=statuses(a).get(mid)
            )
        await shot(b, world.out, "gates-02-after")
        # the orchestrator received the typed answer
        from rework.kernel.state.records import orchestrator_chat_id

        msgs = await a.hub.data.chat_messages(orchestrator_chat_id(mid))
        tool_out = " ".join(str(m.body.get("content", "")) for m in msgs if m.role == "tool")
        if "casual" not in tool_out:
            world.finding(
                "gates", "the typed answer never reached the orchestrator", tool_out=tool_out[-300:]
            )
        for label, app in (("A", a), ("B", b)):
            if app.swallowed:
                world.finding(
                    "errors", f"{label} swallowed", errors=[repr(e) for e in app.swallowed]
                )


@scenario
async def same_chat_two_tuis(world: World) -> None:
    """Both TUIs open the SAME chat. A sends a message; B must show it and its
    reply. Then B sends; A must show B's turn, and the next model turn must
    see the whole conversation (no lost or duplicated turns)."""
    from rework.tui.app import ReworkApp
    from rework.tui.widgets.base import RowList

    world.start_daemon()
    seen_by_model: list[list[str]] = []

    class Recorder(ScriptedChat):
        async def complete(self, request: Any) -> ChatResult:
            seen_by_model.append(
                [
                    str(m.get("content", ""))[:40]
                    for m in request.messages
                    if m.get("role") == "user"
                ]
            )
            return await super().complete(request)

    import rework.kernel.llm.adapters as adapters

    rec = Recorder(
        [
            ChatResult(text="Hello from turn one.", usage=Usage(input_tokens=10, output_tokens=5)),
            ChatResult(text="Reply to B.", usage=Usage(input_tokens=10, output_tokens=5)),
            ChatResult(text="Reply to A again.", usage=Usage(input_tokens=10, output_tokens=5)),
        ]
    )
    adapters.build_provider = lambda entry, extra=None: rec  # type: ignore[assignment]
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        await type_text(pa, "first from A")
        await pa.press("enter")
        await pa.pause(1.0)
        chat_id = a.view("work").convo.id

        # B opens the same chat from its list
        if not await until(pb, lambda: chat_id in {c.id for c in b.hub.snapshot.chats}, 15):
            world.finding("chat/sync", "B's chat list never showed A's chat")
            return
        b.query_one("#chat-list", RowList).activate(chat_id, via_mouse=True)
        await pb.pause(1.5)

        def log_text(app: Any) -> str:
            from rework.tui.widgets.trajectory import Trajectory

            return " ".join(
                str(getattr(s, "renderable", "") or getattr(s, "content", ""))
                for s in app.query_one("#chat-log", Trajectory).query("*")
            )

        if not await until(pb, lambda: "Hello from turn one" in log_text(b), 10):
            world.finding("chat/sync", "B does not show A's turn after opening the chat")
        await type_text(pb, "second from B")
        await pb.press("enter")
        await pb.pause(1.5)
        if not await until(pa, lambda: "second from B" in log_text(a), 10):
            world.finding("chat/sync", "A never shows B's message in the shared chat")
        if not await until(pa, lambda: "Reply to B" in log_text(a), 10):
            world.finding("chat/sync", "A never shows the reply to B")
        if len(seen_by_model) >= 2 and not any("first from A" in u for u in seen_by_model[1]):
            world.finding(
                "chat/history",
                "the model's second turn (sent from B) did not include A's first message",
                second_turn_user_messages=seen_by_model[1],
            )
        # the case that desyncs: A sends AGAIN. A's engine was built before B's
        # turn existed; the model must still see B's message in this turn
        await type_text(pa, "third from A")
        await pa.press("enter")
        await until(pa, lambda: "Reply to A again" in log_text(a), 10)
        if len(seen_by_model) >= 3 and not any("second from B" in u for u in seen_by_model[2]):
            world.finding(
                "chat/history",
                "A's next turn was sent WITHOUT B's message (stale in-memory history)",
                third_turn_user_messages=seen_by_model[2],
            )
        if not await until(pb, lambda: "third from A" in log_text(b), 10):
            world.finding("chat/sync", "B never shows A's third message")
        await shot(a, world.out, "chat-a")
        await shot(b, world.out, "chat-b")
        rows = await a.hub.data.chat_messages(chat_id)
        texts = [r.display for r in rows if r.role in ("user", "assistant")]
        for text in set(texts):
            if texts.count(text) > 1:
                world.finding("chat/history", "a turn is stored twice", text=text[:60])


@scenario
async def tour_every_tab_and_modal(world: World) -> None:
    """Visit every tab and open every form and menu with real seeded data: no
    crash, nothing swallowed, every screen paints something."""
    from rework.tui.app import ReworkApp

    world.script({})
    world.start_daemon()
    import seed

    await seed.seed(world.env["REWORK_DB"])
    await seed.seed_library(world.env["REWORK_DB"], world.env["REWORK_LIBRARY_DIR"])
    a = ReworkApp(poll_seconds=0.5)
    async with a.run_test(size=(140, 40)) as pa:
        await pa.pause(1.5)
        for n, (key, name) in enumerate(
            [
                ("alt+1", "work"),
                ("alt+2", "dashboard"),
                ("alt+3", "gates"),
                ("alt+4", "library"),
                ("alt+5", "cost"),
            ],
            start=1,
        ):
            await pa.press(key)
            await pa.pause(1.2)
            await shot(a, world.out, f"tour-{n:02d}-{name}")
            if a.current_tab != name if hasattr(a, "current_tab") else False:
                world.finding("nav", f"{key} did not open {name}")
        # open a seeded mission on the Dashboard and visit every detail tab
        await pa.press("alt+2")
        await pa.pause(0.8)
        dash = a.view("dashboard")
        missions = [m.id for m in a.hub.snapshot.missions]
        if missions:
            dash.open_mission(missions[0])
            await pa.pause(1.2)
            for tab in ("Trajectory", "Graph", "Output", "Diff", "Report", "Gates", "Chat"):
                hits = [w for w in dash.query("Tab, Btn") if str(getattr(w, "label", "")) == tab]
                if not hits:
                    continue
                await pa.click(hits[0])
                await pa.pause(0.6)
                await shot(a, world.out, f"tour-dash-{tab.lower()}")
        else:
            world.finding("seed", "no seeded missions in the snapshot")
        # every modal opens and closes
        for action, name in (
            ("app.help", "help"),
            ("app.settings", "settings"),
            ("app.new_project", "new-project"),
            ("app.new_library_item", "new-library-item"),
        ):
            before = len(a.screen_stack)
            await a.run_action(action.removeprefix("app."))
            await pa.pause(0.8)
            await shot(a, world.out, f"tour-modal-{name}")
            if len(a.screen_stack) <= before:
                world.finding("modal", f"{name} did not open a screen")
            await pa.press("escape")
            await pa.pause(0.4)
            if len(a.screen_stack) != before:
                world.finding("modal", f"escape did not close {name}", stack=len(a.screen_stack))
        if a.swallowed:
            world.finding("errors", "swallowed", errors=[repr(e) for e in a.swallowed])


@scenario
async def same_chat_turns_on_the_daemon(world: World) -> None:
    """ADR 0021 phase 2b: both TUIs talk to ONE chat and the DAEMON runs every
    turn (the TUIs' own model factory is booby-trapped). Each turn shows once
    in both TUIs, and each model turn sees the whole conversation."""
    from rework.tui.app import ReworkApp
    from rework.tui.widgets.base import RowList

    world.script(
        {
            "You are rework's orchestrator": [
                {"text": "Hello from turn one."},
                {"text": "Reply to B."},
                {"text": "Reply to A again."},
            ]
        }
    )
    world.start_daemon()
    import rework.kernel.llm.adapters as adapters

    def trap(entry: Any, extra: Any = None) -> Any:
        raise AssertionError("the TUI ran a chat turn itself instead of the daemon")

    adapters.build_provider = trap  # type: ignore[assignment]
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=0.5)

    def log_text(app: Any) -> str:
        from rework.tui.widgets.trajectory import Trajectory

        return " ".join(
            str(getattr(s, "renderable", "") or getattr(s, "content", ""))
            for s in app.query_one("#chat-log", Trajectory).query("*")
        )

    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        await type_text(pa, "first from A")
        await pa.press("enter")
        if not await until(pa, lambda: "Hello from turn one" in log_text(a), 15):
            world.finding(
                "chat/daemon",
                "A's turn never got a reply",
                swallowed=[repr(e) for e in a.swallowed],
            )
            await shot(a, world.out, "daemon-chat-a-fail")
            return
        chat_id = a.view("work").convo.id
        await until(pb, lambda: chat_id in {c.id for c in b.hub.snapshot.chats}, 15)
        b.query_one("#chat-list", RowList).activate(chat_id, via_mouse=True)
        await pb.pause(1.5)
        await type_text(pb, "second from B")
        await pb.press("enter")
        await until(pb, lambda: "Reply to B" in log_text(b), 15)
        await type_text(pa, "third from A")
        await pa.press("enter")
        await until(pa, lambda: "Reply to A again" in log_text(a), 15)
        for label, app, pilot, needles in (
            ("A", a, pa, ("second from B", "Reply to B", "Reply to A again")),
            ("B", b, pb, ("third from A", "Reply to A again")),
        ):
            for needle in needles:
                if not await until(
                    pilot, lambda app=app, needle=needle: needle in log_text(app), 10
                ):
                    world.finding("chat/sync", f"{label} never shows {needle!r}")
        for label, app in (("A", a), ("B", b)):
            text = log_text(app)
            for needle in ("first from A", "Reply to B", "Reply to A again"):
                if text.count(needle) > 1:
                    world.finding(
                        "chat/sync", f"{label} shows {needle!r} {text.count(needle)} times"
                    )
            if app.swallowed:
                world.finding(
                    "errors", f"{label} swallowed", errors=[repr(e) for e in app.swallowed]
                )
        await shot(a, world.out, "daemon-chat-a")
        await shot(b, world.out, "daemon-chat-b")


@scenario
async def live_updates_are_fast(world: World) -> None:
    """ADR 0021 phase 3: with the daemon's event stream, a change made by one TUI
    shows in the other within about a second, even with the poll timer slowed
    to 30 s (so polling alone could not explain it)."""
    import time as _time

    from rework.tui.app import ReworkApp

    # chat turns run on the daemon now (ADR 0021 2b): its script answers both
    # the chat (proposal) and the mission orchestrator (park on a question)
    script = _orchestrator_parks_on_owner("Which greeting?")
    script["You are rework's orchestrator"] = [
        {"tool_calls": [{"name": "propose_mission", "arguments": {
            "objective": "pick a greeting", "plan": "echo", "title": "Greeting"}}]}
    ]
    world.script(script)
    world.start_daemon()
    a = ReworkApp(poll_seconds=0.5)
    b = ReworkApp(poll_seconds=30.0)  # B must NOT be saved by its timer
    async with a.run_test(size=(140, 40)) as pa, b.run_test(size=(140, 40)) as pb:
        await pa.pause(0.8)
        await pb.pause(2.0)  # B's first poll + stream connect
        mid = await _launch_one(world, a, pa, "pick a greeting")
        if mid is None:
            return
        await until(pa, lambda: statuses(a).get(mid) == "awaiting_gate", 20)
        await until(pb, lambda: statuses(b).get(mid) == "awaiting_gate", 40)
        t0 = _time.monotonic()
        async with __import__("rework.api.client", fromlist=["DaemonClient"]).DaemonClient() as c:
            await c.mission_verb(mid, "pause")
        seen = await until(pb, lambda: statuses(b).get(mid) == "on_hold", 10)
        lag = _time.monotonic() - t0
        if not seen:
            world.finding("live", "B did not see the pause within 10 s (poll is 30 s)")
        elif lag > 3.0:
            world.finding("live", f"B saw the pause only after {lag:.1f}s")
        else:
            print(f"live update lag: {lag:.2f}s", flush=True)
