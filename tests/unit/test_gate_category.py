"""The PR gate is strict (issue #36, owner decision 2026-11-14): ask_before_pr
is allowed ONLY for four high-stakes categories, with a reason naming one."""

from __future__ import annotations

import pytest

from rework.orchestrator.agent import (
    GATE_CATEGORIES,
    PLAYBOOKS,
    RULES,
    ToolError,
    _gate_ask,
    orchestrator_tools,
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


def test_prompt_text_states_the_strict_rule() -> None:
    playbook = PLAYBOOKS["fix-pr"]
    rules = RULES
    for text in (playbook, rules):
        lowered = text.lower()
        assert "ask_before_pr" in text
        assert "owner asked for is never gated" in lowered
        assert "summary" in text  # where size/unusualness/accepted findings go
        for name in GATE_CATEGORIES:
            assert name in text, f"{name} missing from the prompt text"
    assert "risky, unusual, or you are unsure" not in rules
