"""The PR gate is strict (issue #36, owner decision 2026-11-14): ask_before_pr
is allowed ONLY for four high-stakes categories, with a reason naming one."""

from __future__ import annotations

import pytest

from rework.orchestrator.agent import (
    GATE_CATEGORIES,
    PLAYBOOKS,
    RULES,
    MissionOrchestrator,
    ToolError,
    _gate_ask,
    orchestrator_tools,
    render,
)


def test_the_four_categories_are_the_schema_enum() -> None:
    finish = next(t for t in orchestrator_tools() if t.name == "finish")
    props = finish.input_schema["properties"]
    assert props["gate_category"]["enum"] == list(GATE_CATEGORIES)
    assert set(GATE_CATEGORIES) == {
        "data_loss",
        "security",
        "money",
        "production_infrastructure",
    }
    assert props["gate_category"]["description"]  # documented for the model
    assert "ask_before_pr" in props["gate_category"]["description"]


def test_no_ask_means_no_gate_and_no_category() -> None:
    assert _gate_ask({"outcome": "done", "summary": "s"}) == (False, "", "")
    # a stale category/reason without the flag is ignored, not an error
    assert _gate_ask({"ask_before_pr": False, "gate_category": "security", "reason": "r"}) == (
        False,
        "",
        "",
    )


def test_a_valid_category_and_reason_are_accepted() -> None:
    ask, reason, category = _gate_ask(
        {
            "ask_before_pr": True,
            "gate_category": "security",
            "reason": "rotates the deploy token",
        }
    )
    assert ask is True and category == "security" and "deploy token" in reason


@pytest.mark.parametrize(
    "args",
    [
        {"ask_before_pr": True, "reason": "large diff, many files touched"},
        {"ask_before_pr": True, "reason": "risky"},
        {"ask_before_pr": True, "gate_category": "security"},
        {"ask_before_pr": True, "gate_category": "security", "reason": "   "},
        {"ask_before_pr": True, "gate_category": "big_diff", "reason": "many files"},
        {"ask_before_pr": True, "gate_category": "", "reason": "unsure"},
    ],
)
def test_an_ask_without_a_valid_category_is_refused(args: dict) -> None:
    with pytest.raises(ToolError) as exc:
        _gate_ask(args)
    message = str(exc.value)
    assert "gate_category" in message
    assert "ask_before_pr" in message
    # actionable: tell it to open the PR without a gate, and where the rest goes
    assert "without a gate" in message and "summary" in message


def test_the_refusal_does_not_complete_the_mission() -> None:
    """Validated before self.finished: the live _dispatch path leaves the mission
    unfinished so the orchestrator can retry with no gate."""
    import inspect

    from rework.orchestrator.agent import MissionOrchestrator

    source = inspect.getsource(MissionOrchestrator._dispatch)
    assert source.index("_gate_ask(") < source.index("self.finished = (outcome")


def _replayed(args: dict) -> MissionOrchestrator:
    """MissionOrchestrator reduced to what _recorded_finish reads: the history
    holding one answered `finish` call, i.e. exactly the state a restarted
    mission reloads. No store, bus or model: replay reads only the history."""
    orch = object.__new__(MissionOrchestrator)
    orch.history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "finish", "arguments": args}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "finish", "content": "finished"},
    ]
    orch.accepted_findings = []
    orch.ask_before_pr = False
    orch.pr_reason = ""
    orch.pr_gate_category = ""
    return orch


def test_a_replayed_pre_strictness_finish_cannot_resurrect_a_gate() -> None:
    """Regression: a history record written before the strict rule carries
    ask_before_pr with no gate_category. Replay must downgrade it to no ask
    (and not raise), or a refactor back to the raw args.get() form would let
    old missions open a gate the live rule refuses."""
    orch = _replayed(
        {"outcome": "done", "summary": "s", "ask_before_pr": True, "reason": "large diff"}
    )
    assert orch._recorded_finish() == ("done", "s")  # no ToolError escapes
    assert orch.ask_before_pr is False
    assert (orch.pr_reason, orch.pr_gate_category) == ("", "")


def test_a_replayed_gate_with_a_valid_category_and_reason_is_preserved() -> None:
    orch = _replayed(
        {
            "outcome": "done",
            "summary": "s",
            "ask_before_pr": True,
            "gate_category": "security",
            "reason": "rotates the deploy token",
        }
    )
    assert orch._recorded_finish() == ("done", "s")
    assert orch.ask_before_pr is True
    assert orch.pr_gate_category == "security"
    assert "deploy token" in orch.pr_reason


def test_prompt_text_states_the_strict_rule() -> None:
    # both copies land in the same system prompt, so both must state the rule;
    # render() is what fills $GATE_CATEGORIES in RULES.
    playbook = render(PLAYBOOKS["fix-pr"])
    rules = render(RULES)
    for text in (playbook, rules):
        # RULES is hard-wrapped: normalise whitespace before substring checks
        lowered = " ".join(text.lower().split())
        assert "ask_before_pr" in text
        assert "owner asked for is never gated" in lowered
        assert "summary" in text  # where size/unusualness/accepted findings go
        for name in GATE_CATEGORIES:
            assert name in text, f"{name} missing from the prompt text"
    assert "risky, unusual, or you are unsure" not in rules
