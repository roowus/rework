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
# so the DEFAULT plan is `draft`. These are the plain audits the classifier must
# keep recognising after the change-word matching was tightened to stems.
AUDIT_OBJECTIVES = [
    "audit the code base for dead code",
    "Audit the release process",  # capitalised trigger
    "review the codebase and list the risks",
    "analyse the failure rates in the ledger",
    "please map out the auth flow for me",  # trigger mid-sentence
    "survey the test suite for gaps",
    "write a report on the deploy process",
    "document how the gate rail works",
    # the ten "must stay draft" controls named in the audit of the fix: the
    # stem matching and the continuation rule must not eat real audits
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
    "audit the code base and report back",
    "review the codebase and let me know what you think",
]

# Objectives that name a change, in whatever inflection. The direction matters:
# a real fix mission misrouted to `draft` produces no branch and no PR, so the
# fix silently never lands and the stderr note makes it look intentional. A
# false "draft" is therefore the COSTLIER error, and these all must veto.
CHANGE_OBJECTIVES = [
    "fix the login crash",
    "add a --json flag to rework ls",
    "rename the ledger table",
    "bump the anthropic sdk",
    "migrate the store to postgres",
    "implement retries for push_branch",
    "refactor the delivery module",
    "audit the code base and fix the two crashes",
    # inflections of a change word: matched by stem, not as literal base forms
    # (matching the literals only is what let these through as audits)
    "audit the code base and fixing the two crashes",
    "audit the code base and refactoring the delivery module",
    "audit the code base and adding a --json flag",
    "audit the code base and migrating the store",
    "audit the code base and implementing retries",
    "audit the code base and applying the fixes it recommends",
    "audit the code base and improve error handling",
    "review the code base and rewrite the parser",
    "audit the codebase and delete the unused tables",
    # a coordinating continuation that introduces further work, with verbs no
    # stem list holds: these are caught by the structural rule
    # inflections with no "and" continuation in front of them: only the stem
    # matching can catch these, so they pin FIX 1 on their own
    "fixing the two crashes found in the audit",
    "audit findings: refactoring the delivery module",
    "adding a --json flag, as the audit recommends",
    "the audit recommends migrating the store to postgres",
    "audit the code base and make the tests pass",
    "audit the code base and speed it up",
    # round 3: change verbs that used to sit in the report allowlist
    "audit the code base and write the missing tests",
    "audit the code base and write the missing unit tests",
    "audit the code base and cover the untested branches",
    "audit the code base and include the missing tests",
    "audit the code base and confirm the tests pass",
    "audit the code base and verify the build is green",
    "audit the code base and check that ci passes",
    "audit the code base and trace the crash",
    "audit the code base and find the bug in the parser",
    "audit the code base and identify the crash",
    "audit the code base; write the missing tests",
    "audit the code base and explain the bug, then repair it",
    # the continuation anchor is not only the literal "and"
    "audit the code base and then repair the parser",
    "audit the code base, then repair the parser",
    "audit the code base; then repair the parser",
    "audit the code base. then repair the parser",
    # the same anchors with a verb no stem holds, so ONLY the anchor can catch it
    "audit the code base and then speed it up",
    "audit the code base, then speed it up",
    "audit the code base; then speed it up",
    "audit the code base. then speed it up",
]


@pytest.mark.parametrize("objective", AUDIT_OBJECTIVES)
def test_read_and_report_objectives_default_to_the_draft_plan(objective: str) -> None:
    """`rework new` with no --plan must land on draft for an audit-like
    objective: otherwise the default is `echo`, which does nothing."""
    assert reads_only_plan(objective) == "draft"


@pytest.mark.parametrize("objective", CHANGE_OBJECTIVES)
def test_change_objectives_get_no_plan_suggestion(objective: str) -> None:
    """The classifier is a DEFAULT only: anything that asks for a change stays
    on the caller's plan (the last case is an audit that also asks for a fix)."""
    assert reads_only_plan(objective) is None


def test_the_classifier_errs_toward_a_change_mission() -> None:
    """Which way the mistake leans, asserted rather than assumed: a real fix
    mission misrouted to `draft` produces no branch and no PR, so the fix
    silently never lands and the CLI note makes it look intentional. A missed
    draft suggestion only leaves the caller on its own plan. So an objective
    that can be read either way must read as a CHANGE, and the deliberate
    word collisions resolve that way too."""
    assert len(AUDIT_OBJECTIVES) >= 8 and len(CHANGE_OBJECTIVES) >= 8
    assert "audit the code base and fix the two crashes" in CHANGE_OBJECTIVES
    for ambiguous in (
        "audit the docs and address the gaps",  # "add" also vetoes "address"
        "audit the copy and make it more creative",  # "creat" also vetoes "creative"
        "review the module and remove the dead branch",
        "audit the code base and then fix what you find",  # "and then"
    ):
        assert reads_only_plan(ambiguous) is None, ambiguous


def test_both_change_signals_are_load_bearing() -> None:
    """Two independent mechanisms, one case each, so neither can rot silently:
    the stem/form alternation catches a known change word, and the continuation
    rule catches a novel verb that no list holds."""
    from rework.missions import _has_change_veto, _has_further_work

    inflected = "audit the code base and the delivery module needs refactoring"
    assert _has_change_veto(inflected) and not _has_further_work(inflected)
    novel_verb = "audit the code base and speed it up"
    assert _has_further_work(novel_verb) and not _has_change_veto(novel_verb)


def test_report_phrases_and_forms_do_not_false_veto() -> None:
    """ "write a report" is a trigger phrase, stripped before the scan, and
    "coverage" is not the change verb "cover"; yet a bare "write" or "cover"
    still vetoes."""
    from rework.missions import _has_change_veto

    assert not _has_change_veto("write a report on test coverage")
    assert not _has_change_veto("audit it and tell me what you find")
    assert _has_change_veto("audit it and write the tests")
    assert _has_change_veto("audit it, covering the branches")


def test_report_only_words_hold_no_change_verb() -> None:
    """The allowlist is fail-safe only while it holds verbs that can never head
    real work."""
    from rework.missions import REPORT_ONLY_WORDS

    for verb in (
        "write", "include", "cover", "confirm", "verify", "check", "find",
        "identify", "trace", "repair", "fix",
    ):  # fmt: skip
        assert verb not in REPORT_ONLY_WORDS, verb


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
