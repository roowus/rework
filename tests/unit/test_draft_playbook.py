"""The `draft` playbook: document work with no PR gate.

Two things must hold for it to be usable at all: the orchestrator is actually
told about it (the system prompt AND the tool enum the model must satisfy), and
it produces NO open_pr gate -- a writing mission's deliverable is its Report tab,
so pushing a branch and opening a PR would be wrong behaviour, not just
unnecessary.
"""

from __future__ import annotations

import pytest

from rework.missions import PLANS, PLAYBOOK_GATES, orchestrated_plan, reads_only_plan
from rework.orchestrator.agent import PLAYBOOKS
from rework.orchestrator.chat import PROPOSE_TOOL, SYSTEM_PROMPT


def test_draft_is_a_known_playbook() -> None:
    assert "draft" in PLAYBOOKS
    assert "draft" in PLANS
    assert "draft" in PLAYBOOK_GATES


def test_draft_text_instructs_research_draft_review_revise() -> None:
    text = PLAYBOOKS["draft"].lower()
    assert "web_search" in text and "fetch_url" in text, "roles must research with the web tools"
    assert "source" in text, "every claim must be recorded against a citation"
    assert "auditor" in text, "the draft is reviewed before it counts"
    assert "constraint" in text or "length" in text, "stated constraints are checked"
    assert "invent" in text or "assum" in text, "details about the owner must not be guessed"
    assert "revise" in text, "audit findings loop back into a revision"
    assert "ask_owner" in text, "facts only the owner knows are asked, not guessed"


def test_draft_text_says_no_push_no_pr() -> None:
    text = PLAYBOOKS["draft"]
    assert "NO BRANCH PUSH AND NO PR" in text
    assert "Report tab" in text, "the deliverable is the mission's report, not repo files"
    assert "do not write files into the target repo" in text
    assert "write the deliverables into the project" not in text


def test_draft_yields_no_open_pr_gate() -> None:
    assert PLAYBOOK_GATES["draft"] == []
    plan = orchestrated_plan("draft", "pro")
    assert plan["playbook"] == "draft"
    assert plan["gates"] == [], "a writing mission must not offer to open a pull request"
    assert "open_pr" not in plan["gates"]
    assert "ship" not in plan["gates"], "there is no diff to ship"


def test_fix_pr_still_publishes_and_fix_still_ships() -> None:
    """Negative control for the assertion above: fix-pr still publishes a PR
    (no gate by default since #36) and fix still ends at the ship gate."""
    fix_pr = orchestrated_plan("fix-pr", "pro")
    assert fix_pr["gates"] == []
    assert fix_pr["publish"] == "pr"
    assert orchestrated_plan("fix", "pro")["gates"] == ["ship"]


def _plan_listing() -> list[str]:
    """The 'Plans you can choose' entries, continuation lines folded in."""
    lines = SYSTEM_PROMPT.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Plans you can choose"))
    entries: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        if line.startswith("- "):
            entries.append(line[2:])
        elif entries:
            entries[-1] += " " + line.strip()
    return entries


def test_chat_prompt_lists_draft_with_a_hint() -> None:
    entries = _plan_listing()
    draft = next((e for e in entries if e.startswith("draft")), "")
    assert draft, f"the chat must be told draft exists: {entries}"
    assert "writing" in draft, f"the hint must say what draft is for: {draft!r}"
    assert "not code" in draft
    # and the negative control: the other plans are still listed
    names = {e.split()[0] for e in entries}
    assert names == set(PLANS), f"plan listing drifted from PLANS: {names}"


def test_chat_proposal_tool_accepts_draft() -> None:
    enum = PROPOSE_TOOL.input_schema["properties"]["plan"]["enum"]
    assert "draft" in enum
    assert set(enum) == set(PLANS), "the tool enum and the accepted plans must not drift"


@pytest.mark.parametrize("playbook", sorted(PLAYBOOKS))
def test_every_playbook_has_a_gate_entry(playbook: str) -> None:
    """A missing entry silently falls back to 'no gates' -- the bug that would
    make `draft` produce PR behaviour if the tables ever diverge."""
    assert playbook in PLAYBOOK_GATES
    assert set(PLAYBOOK_GATES) == set(PLAYBOOKS)


# Every objective below is a read-and-report job: the deliverable is writing,
# so the DEFAULT plan is `draft`. The shape is closed: a read-only opener, no
# change verb anywhere, no second clause (bar a closed trailing report phrase).
AUDIT_OBJECTIVES = [
    "audit the release process",
    "review the codebase for security issues",
    "analyse the auth flow",
    "map out the module boundaries",
    "survey how errors propagate",
    "write a report on test coverage",
    "document how the scheduler works",
    "investigate what causes the flaky test",
    "explain how retries interact",
    "audit the doubletake code base and tell me what you find",  # the incident
    "review the code base",
    "audit the code base and report back",
    "document how the retry logic works, and let me know",
    # shape variants: capitals, a leading "please", a trailing full stop
    "Audit the release process",
    "please map out the auth flow",
    "Please review the code base.",
    "audit the code base for dead code",
    "write up a report on the deploy process",
    # item 4: the "let me know what X" twins of the closed "tell me what X"
    # phrases; both are END-anchored single phrases like the rest
    "review the codebase and let me know what you think",
    "review the codebase and let me know what you find",
]

# Objectives that need a code change, or that the closed shape cannot prove are
# read-only. The direction matters: a real fix mission misrouted to `draft`
# produces no branch and no PR, so the fix silently never lands and the stderr
# note makes it look intentional. All of these must return None.
CHANGE_OBJECTIVES = [
    "audit the code base and fix the two crashes",
    "audit the code base, kill the flaky test",
    "audit the code base and also kill the flaky test",
    "audit the code base and only keep the stable deps",
    "audit the code base: kill the flaky test",
    "audit the code base - kill the flaky test",
    "audit the tests, speed them up",
    "review the codebase, tighten the push timeouts",
    "review the codebase, prune the dead flags",
    "survey the schema, backfill the new column",
    "map out the modules then restructure them",
    "audit the deploy scripts, automate the rollback",
    "audit the code base, then repair the parser",
    "audit the auth flow to close the gaps",
    "audit the code base to stop the flaky test",
    "audit the code base so the flaky test stops failing",
    "audit the code base and outline the redesign",
    "audit the code base and mention what should be tightened",
    "audit the code base and fixing the two crashes",
    "audit the code base and refactoring the delivery module",
    "add a survey endpoint",
    "explain how the auth works and fix the leak",
    "document how to bump the version",
    "review the code base and fix it",
    "fix the flaky test",
    "write the missing tests",
    "investigate what causes the crash and fix it",
    "audit the logs rotation",
    "summarise the risks",
    # no opener at all
    "add a --json flag to rework ls",
    "rename the ledger table",
    "migrate the store to postgres",
    # a second clause that is not a closed trailing phrase
    "review the codebase and list the risks",
    "audit the code base. then speed it up",
    "audit the code base; then repair the parser",
    "audit the code base (and fix it)",
    "audit the code base and tell me what you find, then fix it",
    # a purpose or a condition with no separator character in front of it
    "audit the code base until the tests pass",
    "review the code base such that it compiles",
    "audit the flaky test away",
]


@pytest.mark.parametrize("objective", AUDIT_OBJECTIVES)
def test_read_and_report_objectives_default_to_the_draft_plan(objective: str) -> None:
    """`rework new` with no --plan must land on draft for an audit-like
    objective: otherwise the default is `echo`, which does nothing."""
    assert reads_only_plan(objective) == "draft"


@pytest.mark.parametrize("objective", CHANGE_OBJECTIVES)
def test_change_objectives_get_no_plan_suggestion(objective: str) -> None:
    """The classifier is a DEFAULT only: anything that asks for a change stays
    on the caller's plan."""
    assert reads_only_plan(objective) is None


def test_the_classifier_errs_toward_a_change_mission() -> None:
    """Which way the mistake leans, asserted rather than assumed: a real fix
    mission misrouted to `draft` produces no branch and no PR, so the fix
    silently never lands. A missed draft suggestion only leaves the caller on
    its own plan. So an input that can be read either way must read as None,
    and the over-generous denylist collisions resolve that way too."""
    assert len(AUDIT_OBJECTIVES) >= 8 and len(CHANGE_OBJECTIVES) >= 8
    for ambiguous in (
        "audit the docs and address the gaps",  # "add" also hits "address"
        "audit the copy and make it more creative",  # "creat" also hits "creative"
        "review the module and remove the dead branch",
        "audit the code base and then fix what you find",
        "audit the portfolio page",  # "port" also hits "portfolio"
    ):
        assert reads_only_plan(ambiguous) is None, ambiguous


def test_an_opener_must_start_the_objective() -> None:
    """The trigger is anchored at the START (after an optional "please"): an
    opener appearing later in the string is not a read-only objective, so the
    anchor is load-bearing and pinned here."""
    assert reads_only_plan("also audit the code base") is None
    assert reads_only_plan("now review the parser") is None
    # end to end: the objective above is a draft, so the anchor is what does it
    assert reads_only_plan("audit the code base") == "draft"


def test_the_denylist_is_consulted_over_the_whole_objective() -> None:
    """A change verb in the middle of an otherwise clean audit, with no clause
    separator to point at it, still returns None; remove the verb and it is a
    draft."""
    assert reads_only_plan("audit how the team will tighten the code base") is None
    assert reads_only_plan("audit how the team reads the code base") == "draft"
    # the verb can also come from `text`, which is scanned for verbs only
    assert reads_only_plan("audit the release process", "also fix the typo") is None


def test_only_one_closed_trailing_phrase_is_stripped_from_the_end() -> None:
    """The phrases are closed and END-anchored: one is dropped, and a phrase in
    the middle or a second one still leaves a clause boundary behind."""
    assert reads_only_plan("audit the code base and tell me") == "draft"
    assert reads_only_plan("audit the code base and summarize the findings") == "draft"
    assert reads_only_plan("audit the code base and let me know about it") is None
    assert reads_only_plan("audit the code base and report back and let me know") is None
    assert reads_only_plan("audit the code base and tell me what you find first") is None


def test_the_chat_defaults_a_missing_plan_but_never_overrides_a_named_one() -> None:
    """The launching chat is where the incident came from: a proposal with NO
    plan falls back to the classifier's default, a plan the model named stays."""
    from rework.orchestrator.chat import proposal_plan

    audit = "audit the release process"
    assert proposal_plan({}, audit) == "draft"
    assert proposal_plan({"plan": ""}, audit) == "draft"
    assert proposal_plan({"plan": "fix"}, audit) == "fix"  # named: never overridden
    assert proposal_plan({"plan": "echo"}, audit) == "echo"
    assert proposal_plan({}, "audit the code base and write the missing tests") == "fix"
    assert proposal_plan({}, "fix the login crash") == "fix"


async def test_a_draft_mission_needs_no_repo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Found live (TUI e2e 2026-11-26): the chat proposes `draft` for research
    ("write a report on X"), the card offers Launch, and launching failed with
    "--plan draft requires a repo". Draft's deliverable is the Report tab and
    its roles research with the web tools; fix/fix-pr/audit still need a repo."""
    from rework.kernel.state.sqlite import open_sqlite
    from rework.missions import create_mission

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        mission = await create_mission(
            "write a short report on X", plan="draft", store=store, orchestrator_model="flash"
        )
        assert mission.repo_path is None and mission.repo_origin is None
        for plan in ("fix", "fix-pr", "audit"):
            with pytest.raises(ValueError, match="requires a repo"):
                await create_mission("change code", plan=plan, store=store)
    finally:
        await store.aclose()
