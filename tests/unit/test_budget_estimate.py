"""`estimate_budget`: the opening cap follows the SIZE of the task, not the
deployment default. Pure, predictable (same text -> same answer), never raises.
"""

from __future__ import annotations

from typing import Any

import pytest

from rework.budget_policy import estimate_budget

RISKY_TEXTS = [
    "migrate the missions table to the new layout",
    "add a migration for the charges table",
    "change the schema of projects",
    "fix the auth flow",
    "harden authentication for the API",
    "add permission checks to the ship gate",
    "security review of the transport",
    "rewrite the scheduler",
    "refactor everything in the kernel",
    "make every role retry",
    "replace all of the config loaders",
    "rework the architecture",
    "fix the concurrency bug in the pool",
    "fix the data race in the bus",
    "improve performance across the whole pipeline",
]

LONG = "add a feature " * 40  # ~560 chars: more than one file


@pytest.mark.parametrize(
    "text",
    [
        "fix a typo",
        "typo in the readme",
        "update the comments",
        "rename the helper",
        "bump version",
    ],
)
def test_docs_sized_text_is_at_most_two_dollars(text: str) -> None:
    usd, reason = estimate_budget(text)
    assert usd <= 2.0
    assert "docs/typo" in reason


def test_a_one_liner_costs_one_dollar_and_a_longer_docs_change_two() -> None:
    assert estimate_budget("fix typo")[0] == 1.0
    assert estimate_budget("fix the typo in the docs, " * 8)[0] == 2.0


@pytest.mark.parametrize("text", ["add a feature to the parser", "wire the new flag through"])
def test_normal_feature_text_is_three_to_five_dollars(text: str) -> None:
    usd, reason = estimate_budget(text)
    assert 3.0 <= usd <= 5.0
    assert "feature" in reason


def test_a_longer_brief_is_the_top_of_the_feature_band() -> None:
    assert estimate_budget(LONG)[0] == 5.0


@pytest.mark.parametrize("text", RISKY_TEXTS)
def test_each_risky_signal_costs_at_least_eight_dollars(text: str) -> None:
    usd, reason = estimate_budget(text)
    assert usd >= 8.0
    assert "risky" in reason


def test_several_risky_signals_cost_ten() -> None:
    assert estimate_budget("migrate the auth schema")[0] == 10.0


def test_four_numbered_requirements_escalate() -> None:
    text = "do this:\n1. one\n2. two\n3. three\n4. four"
    usd, reason = estimate_budget(text)
    assert usd == 8.0
    assert "numbered requirements" in reason
    # three is still a normal feature
    assert estimate_budget("1. one\n2. two\n3. three")[0] <= 5.0


def test_a_long_brief_escalates() -> None:
    assert estimate_budget("add a thing. " * 120)[0] == 8.0  # ~1560 chars, no signal
    assert estimate_budget("add a thing " * 300)[0] == 10.0  # > 3000 chars


def test_risky_outranks_smallness() -> None:
    """ "auth docs" is a large job, not a docs job."""
    assert estimate_budget("write the auth docs")[0] >= 8.0


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_text_defaults_to_two_and_never_raises(text: str) -> None:
    usd, reason = estimate_budget(text)
    assert usd == 2.0
    assert "no task text" in reason


def test_non_text_input_does_not_raise() -> None:
    assert estimate_budget(None)[0] == 2.0  # type: ignore[arg-type]


def test_cost_history_nudges_inside_the_band_only() -> None:
    # a shop whose missions finish around $2 pulls a long feature down to the
    # band's floor, but a docs change cannot go below it and a risky one stays
    assert estimate_budget(LONG, cost_history=2.0)[0] == 3.0
    assert estimate_budget("fix a typo", cost_history=100.0)[0] == 2.0
    assert estimate_budget("migration of the schema", cost_history=0.2)[0] == 8.0


def test_garbage_cost_history_is_ignored() -> None:
    plain = estimate_budget("add a feature")
    for garbage in (float("nan"), float("inf"), -5.0, 0, "lots", True):
        assert estimate_budget("add a feature", cost_history=garbage) == plain  # type: ignore[arg-type]


def test_every_answer_is_rounded_to_half_a_dollar_and_never_below_it() -> None:
    for text in [*RISKY_TEXTS, LONG, "fix typo", "", "add a feature", "1. a\n2. b\n3. c\n4. d"]:
        for history in (None, 0.7, 3.3, 42.0):
            usd, _ = estimate_budget(text, cost_history=history)
            assert usd >= 0.5
            assert round(usd * 2) == usd * 2


def test_it_is_deterministic() -> None:
    text = "migrate the schema and fix the auth flow"
    assert estimate_budget(text) == estimate_budget(text)


# -- calibrating from what missions actually cost ---------------------------
#
# requirement 1: "calibrate from the real costs in the charges table". The
# estimate's bands say how big a task LOOKS; this says what missions cost here.


class _FakeStore:
    """The two store calls the calibration makes, nothing else: finished
    missions and their ledger totals (that is all `typical_finished_cost`
    reads, so no sqlite is needed)."""

    def __init__(self, finished: dict[str, float], spend_raises: bool = False) -> None:
        self.finished = finished
        self.spend_raises = spend_raises

    async def list_missions(self, status: Any = None, limit: int = 50) -> list[Any]:
        from rework.kernel.state.records import MissionStatus

        if status is not MissionStatus.DONE:
            return []
        return [_Row(mid) for mid in self.finished][:limit]

    async def mission_spend(self, mission_id: str) -> Any:
        from rework.kernel.state.records import BudgetState

        if self.spend_raises:
            raise RuntimeError("the ledger is unreadable")
        return BudgetState(spent_usd=self.finished[mission_id])


class _Row:
    def __init__(self, mission_id: str) -> None:
        self.id = mission_id


async def test_typical_finished_cost_is_the_median_of_finished_missions() -> None:
    from rework.budget_policy import typical_finished_cost

    store = _FakeStore({"a": 1.0, "b": 2.0, "c": 9.0})
    assert await typical_finished_cost(store) == 2.0
    assert await typical_finished_cost(_FakeStore({"a": 1.0, "b": 3.0})) == 2.0  # even count


async def test_typical_finished_cost_is_none_without_history_and_never_raises() -> None:
    from rework.budget_policy import typical_finished_cost

    assert await typical_finished_cost(_FakeStore({})) is None
    assert await typical_finished_cost(_FakeStore({"a": 4.0}, spend_raises=True)) is None
    assert await typical_finished_cost(_FakeStore({"a": 0.0, "b": 0.0})) is None  # no real cost


async def test_a_finished_mission_average_nudges_the_estimate_within_band() -> None:
    """A shop whose missions really finish around $3 gets $4.50 for a long
    feature brief instead of the band's $5 — and still $1-2 for a typo, $8 for
    a risky change: the calibration never crosses a band."""
    from rework.budget_policy import typical_finished_cost

    history = await typical_finished_cost(_FakeStore({"a": 2.0, "b": 4.0}))
    assert history == 3.0
    assert estimate_budget(LONG)[0] == 5.0
    assert estimate_budget(LONG, cost_history=history)[0] == 4.5
    assert estimate_budget("fix the typo", cost_history=history)[0] == 2.0  # docs band
    assert estimate_budget("migrate the schema", cost_history=history)[0] == 8.0
    # ...and with no history at all the answer is exactly what it was before
    assert estimate_budget(LONG, cost_history=await typical_finished_cost(_FakeStore({}))) == (
        estimate_budget(LONG)
    )
