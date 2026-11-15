"""Mission search: the dashboard's text filter and the ✦ AI ranking.

Pure functions, so no app, no store: a tiny stand-in Mission carries only the
five filterable fields (id, title, objective, project_id, status)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from rework.kernel.llm.types import ChatResult
from rework.tui import search


def mission(
    id: str,
    objective: str,
    *,
    title: str | None = None,
    project_id: str | None = None,
    status: str = "running",
    created_at: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        objective=objective,
        title=title,
        project_id=project_id,
        status=SimpleNamespace(value=status),
        created_at=created_at,
    )


MISSIONS = [
    mission("m-flaky", "fix the flaky rolling-mean test", project_id="p-polystats", status="done"),
    mission("m-doc", "write the search docs", title="Mission search", project_id="p-rework"),
    mission("m-gate", "audit the budget ledger", project_id="p-rework", status="awaiting_gate"),
]


def ids(missions: list[Any]) -> list[str]:
    return [m.id for m in missions]


# -- text search ------------------------------------------------------------


def test_empty_query_keeps_everything_in_order() -> None:
    assert ids(search.search_missions(MISSIONS, "")) == ["m-flaky", "m-doc", "m-gate"]
    assert ids(search.search_missions(MISSIONS, "   ")) == ["m-flaky", "m-doc", "m-gate"]


def test_substring_is_case_insensitive() -> None:
    assert ids(search.search_missions(MISSIONS, "FLAKY")) == ["m-flaky"]
    assert ids(search.search_missions(MISSIONS, "Ledger")) == ["m-gate"]


def test_all_tokens_must_match_somewhere() -> None:
    assert ids(search.search_missions(MISSIONS, "flaky test")) == ["m-flaky"]
    # tokens may match different fields (title + project)
    assert ids(search.search_missions(MISSIONS, "search rework")) == ["m-doc"]
    assert ids(search.search_missions(MISSIONS, "flaky ledger")) == []


def test_matches_id_project_and_status() -> None:
    assert ids(search.search_missions(MISSIONS, "m-gate")) == ["m-gate"]
    assert ids(search.search_missions(MISSIONS, "polystats")) == ["m-flaky"]
    assert ids(search.search_missions(MISSIONS, "awaiting")) == ["m-gate"]
    assert ids(search.search_missions(MISSIONS, "done")) == ["m-flaky"]


def test_fuzzy_subsequence_is_the_fallback() -> None:
    """'flky' is no substring of anything: it only hits as a subsequence."""
    assert ids(search.search_missions(MISSIONS, "flky")) == ["m-flaky"]
    assert ids(search.search_missions(MISSIONS, "zzzz")) == []


def test_literal_hits_rank_above_fuzzy_ones() -> None:
    literal = mission("m-1", "ledger audit")
    fuzzy = mission("m-2", "load edge request")  # only l…e…d…g…e…r in order
    assert ids(search.search_missions([fuzzy, literal], "ledger")) == ["m-1", "m-2"]


# -- summaries & prompt -----------------------------------------------------


def test_summarize_clips_the_objective_and_takes_one_run_line() -> None:
    long = mission("m-long", "x" * 300)
    run = SimpleNamespace(artifact={"summary": "y" * 300, "findings": [{"title": "nope"}]})
    summary = search.summarize(long, [run])
    assert summary["id"] == "m-long"
    assert len(summary["objective"]) == search.SUMMARY_CLIP
    assert summary["objective"].endswith("…")
    assert summary["output"].startswith("yyy") and len(summary["output"]) == 100


def test_summarize_falls_back_to_kind_and_tolerates_no_runs() -> None:
    assert search.summarize(mission("m-1", "obj"), [])["output"] == ""
    run = SimpleNamespace(artifact={"kind": "code_change"})
    assert search.summarize(mission("m-1", "obj"), [run])["output"] == "code_change"


def test_build_ai_prompt_contains_ids_query_and_stays_small() -> None:
    summaries = [search.summarize(m) for m in MISSIONS]
    prompt = search.build_ai_prompt("flaky floats", summaries)
    assert "flaky floats" in prompt
    for m in MISSIONS:
        assert m.id in prompt
    assert "JSON" in prompt

    many = [
        {"id": f"m-{i}", "project": "", "status": "done", "objective": "o", "output": ""}
        for i in range(search.MAX_CANDIDATES + 25)
    ]
    capped = search.build_ai_prompt("q", many)
    assert len(capped.splitlines()) < search.MAX_CANDIDATES + 10
    assert f"m-{search.MAX_CANDIDATES + 24}" not in capped


def test_candidates_prefilters_only_over_the_cap() -> None:
    few = [mission(f"m-{i}", f"obj {i}", created_at=float(i)) for i in range(5)]
    assert search.candidates(few, "obj") == few

    many = [mission(f"m-{i}", f"obj {i}", created_at=float(i)) for i in range(100)]
    picked = search.candidates(many, "obj 42")
    assert len(picked) == 1 and picked[0].id == "m-42"

    # nothing matches the text search: the newest win
    newest = search.candidates(many, "zzz")
    assert len(newest) == search.MAX_CANDIDATES
    assert newest[0].id == "m-99"


# -- parsing the ranking ----------------------------------------------------


def test_parse_plain_and_fenced_json() -> None:
    known = {"m-1", "m-2"}
    plain = '[{"id": "m-1", "reason": "the one"}]'
    fenced = 'Here you go:\n```json\n[{"id": "m-1", "reason": "the one"}]\n```\nEnjoy.'
    assert search.parse_ai_ranking(plain, known) == [{"id": "m-1", "reason": "the one"}]
    assert search.parse_ai_ranking(fenced, known) == search.parse_ai_ranking(plain, known)


def test_parse_drops_unknown_ids_and_duplicates() -> None:
    text = (
        '[{"id": "m-1", "reason": "first"}, {"id": "ghost", "reason": "?"}, '
        '{"id": "m-3", "reason": "unknown"}, {"id": "m-1", "reason": "again"}]'
    )
    assert search.parse_ai_ranking(text, {"m-1", "m-3"}) == [
        {"id": "m-1", "reason": "first"},
        {"id": "m-3", "reason": "unknown"},
    ]


def test_parse_clips_reasons_and_caps_the_list() -> None:
    rows = [{"id": f"m-{i}", "reason": "r" * 400} for i in range(9)]
    text = str(rows).replace("'", '"')
    out = search.parse_ai_ranking(text, {f"m-{i}" for i in range(9)})
    assert len(out) == search.MAX_RESULTS
    assert all(len(r["reason"]) <= search.REASON_CLIP for r in out)


def test_parse_returns_empty_on_garbage() -> None:
    for junk in ["", "no idea, sorry", "[]", "[not json", '{"error": "nope"}', "null"]:
        assert search.parse_ai_ranking(junk, {"m-1"}) == []


# -- ai_search against a fake provider -------------------------------------


class _Provider:
    name = "fake"

    def __init__(self, reply: str = "", delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ChatResult:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatResult(text=self.reply)


async def test_ai_search_returns_the_ranking_and_sends_a_small_request() -> None:
    provider = _Provider('Sure:\n```json\n[{"id": "m-flaky", "reason": "closest"}]\n```')
    summaries = [search.summarize(m) for m in MISSIONS]
    out = await search.ai_search(provider, "model-x", "flaky", summaries)
    assert out == [{"id": "m-flaky", "reason": "closest"}]
    request = provider.requests[0]
    assert request.model == "model-x"
    assert request.max_tokens <= 500
    assert "m-flaky" in request.messages[0]["content"]


async def test_ai_search_times_out_with_a_clean_error() -> None:
    provider = _Provider('[{"id": "m-1", "reason": "late"}]', delay=1.0)
    with pytest.raises(search.AISearchError, match="timed out"):
        await search.ai_search(provider, "m", "q", [{"id": "m-1"}], timeout=0.05)


async def test_ai_search_wraps_a_provider_failure() -> None:
    class _Boom:
        name = "fake"

        async def complete(self, request: Any) -> ChatResult:
            raise RuntimeError("503 upstream")

    with pytest.raises(search.AISearchError, match="503 upstream"):
        await search.ai_search(_Boom(), "m", "q", [{"id": "m-1"}])


# -- model choice -----------------------------------------------------------


def test_pick_entry_is_the_cheapest_with_credentials() -> None:
    entries = [
        SimpleNamespace(
            id="pro",
            tier=3,
            cost_in_per_mtok=3.0,
            cost_out_per_mtok=9.0,
            has_credentials=lambda: True,
        ),
        SimpleNamespace(
            id="flash",
            tier=1,
            cost_in_per_mtok=0.2,
            cost_out_per_mtok=1.1,
            has_credentials=lambda: True,
        ),
        SimpleNamespace(
            id="nokey",
            tier=0,
            cost_in_per_mtok=0.0,
            cost_out_per_mtok=0.0,
            has_credentials=lambda: False,
        ),
    ]
    catalog = SimpleNamespace(all=lambda: entries)
    assert search.pick_entry(catalog).id == "flash"


def test_pick_entry_is_none_without_credentials() -> None:
    entries = [
        SimpleNamespace(
            id="haiku",
            tier=1,
            cost_in_per_mtok=1.0,
            cost_out_per_mtok=2.0,
            has_credentials=lambda: False,
        ),
    ]
    assert search.pick_entry(SimpleNamespace(all=lambda: entries)) is None
    assert search.pick_entry(SimpleNamespace(all=lambda: [])) is None
