"""The configurable default/escalation model, end to end at the unit level.

Hermetic: fake catalogs, monkeypatched env, no network and no real HOME. The
mission's point is that NO default path hardcodes a model id, so these tests
assert both what resolves today and that changing the env vars changes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rework.config import (
    DEFAULT_MODEL_ENV,
    DEFAULT_MODEL_ID,
    ESCALATION_MODEL_ENV,
    ESCALATION_MODEL_ID,
    default_model_id,
    escalation_model_id,
)
from rework.kernel.llm.catalog import (
    ModelCatalog,
    ModelEntry,
    api_key_env_name,
    is_loopback_url,
    version_key,
)


def _entry(catalog_id: str, *, tier: int = 1, provider: str = "openai-compatible", **kw: Any):
    return ModelEntry(
        id=catalog_id,
        provider=provider,
        model=f"{catalog_id}-wire",
        tier=tier,
        api_key_env=kw.pop("api_key_env", "TEST_KEY"),
        base_url=kw.pop("base_url", "https://api.example.com/v1"),
        **kw,
    )


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (DEFAULT_MODEL_ENV, ESCALATION_MODEL_ENV, "TEST_KEY", "FLASH_KEY", "CHEAP_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_KEY", "x")


# 1. the default model ----------------------------------------------------------


def test_default_honours_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2)])
    assert default_model_id(cat) == DEFAULT_MODEL_ID == "flash"
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "pro")
    assert default_model_id(cat) == "pro"


def test_default_falls_back_to_the_cheapest_usable_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 'flash' is in the catalog but THIS machine has no key for it: the default
    # is the cheapest entry it can actually call, not a dead id
    cat = ModelCatalog(
        [
            _entry("flash", tier=1, api_key_env="FLASH_KEY"),
            _entry("cheap", tier=0, api_key_env="CHEAP_KEY"),
        ]
    )
    monkeypatch.setenv("CHEAP_KEY", "y")
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "flash")
    assert default_model_id(cat) == "cheap"
    assert default_model_id(cat) == default_model_id(cat)  # deterministic


def test_default_env_naming_a_missing_id_falls_back_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = ModelCatalog([_entry("flash", tier=1), _entry("cheap", tier=0)])
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "nope")
    assert default_model_id(cat) == "cheap"


# 2. the escalation model -------------------------------------------------------


def test_escalation_honours_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry(ESCALATION_MODEL_ID, tier=3),
            _entry("opus", tier=4),
        ]
    )
    assert escalation_model_id(cat) == ESCALATION_MODEL_ID == "claude-sonnet-5-5"
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "opus")
    assert escalation_model_id(cat) == "opus"
    # the env var is the ONLY knob: no id is banned, and an id the catalog does
    # not hold falls back to the strongest usable rather than raising
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "not-in-the-catalog")
    assert escalation_model_id(cat) == "opus"


def test_escalation_without_a_key_falls_back_to_the_strongest_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = ModelCatalog(
        [
            _entry("claude-sonnet-5-5", tier=3, api_key_env="FLASH_KEY"),
            _entry("flash", tier=1),
        ]
    )
    monkeypatch.delenv("FLASH_KEY", raising=False)
    assert escalation_model_id(cat) == "flash"  # the strongest this machine can call


# 3. the tie-break: equal tier AND price -> the NEWER model ----------------------


def _twins(order: list[str]) -> ModelCatalog:
    specs = {
        "claude-sonnet-5": {"tier": 3, "cost_in_per_mtok": 3.0, "cost_out_per_mtok": 15.0},
        "claude-sonnet-5-5": {"tier": 3, "cost_in_per_mtok": 3.0, "cost_out_per_mtok": 15.0},
    }
    return ModelCatalog([_entry(name, **specs[name]) for name in order])


def test_tie_break_prefers_the_newer_model_in_both_catalog_orders() -> None:
    for order in (
        ["claude-sonnet-5", "claude-sonnet-5-5"],
        ["claude-sonnet-5-5", "claude-sonnet-5"],
    ):
        cat = _twins(order)
        assert cat.strongest_usable().id == "claude-sonnet-5-5", order
        assert cat.cheapest_usable().id == "claude-sonnet-5-5", order


def test_a_dated_id_reads_as_newer_than_its_undated_sibling() -> None:
    assert version_key("claude-sonnet-5-20250929") > version_key("claude-sonnet-5")
    cat = ModelCatalog(
        [
            _entry("claude-sonnet-5", tier=3),
            _entry("claude-sonnet-5-20250929", tier=3),
        ]
    )
    assert cat.strongest_usable().id == "claude-sonnet-5-20250929"


def test_ids_without_digits_do_not_crash_the_ranking() -> None:
    # no digits -> an all-zero key of the SAME width as a parsed one, so the
    # tuple comparison against `claude-sonnet-5` stays well-defined
    assert version_key("flash") == version_key("pro") == (0,) * len(version_key("x-1"))
    assert isinstance(version_key("flash"), tuple)
    # the ranking is still TOTAL (no raise) and deterministic: with nothing else
    # to separate them the final tie-break is the id, so the catalog ORDER
    # cannot change the answer (cheap rank ascends the id, strong rank descends)
    picks = []
    for order in (["pro", "flash"], ["flash", "pro"]):
        cat = ModelCatalog([_entry(name, tier=2) for name in order])
        picks.append((cat.cheapest_usable().id, cat.strongest_usable().id))
    assert picks == [("flash", "pro"), ("flash", "pro")]


# 4. no default path yields 'pro' (or any hardcoded id) --------------------------


def test_roles_and_plans_carry_no_hardcoded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.missions import PLAN_FIX
    from rework.roles.registry import autodiscover

    registry = autodiscover()
    for name in ("investigator", "coder", "auditor"):
        assert registry.get(name).default_model == "", f"{name} still hardcodes a model"
    assert not [s for s in PLAN_FIX["steps"] if "model" in s], "a plan step still hardcodes a model"

    import inspect

    from rework.evals import harness

    assert '"model"' not in inspect.getsource(harness), "the eval harness still hardcodes a model"


def test_every_role_resolves_to_the_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.roles.registry import autodiscover

    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2), _entry("fake", tier=0)])
    registry = autodiscover()
    ctx = SimpleNamespace(run=SimpleNamespace(model_ref={}), catalog=cat)
    for name in ("investigator", "coder", "auditor"):
        assert registry.get(name).resolve_model(ctx) == "flash", name
    # nothing is hardcoded: the SAME roles follow the env var
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "pro")
    for name in ("investigator", "coder", "auditor"):
        assert registry.get(name).resolve_model(ctx) == "pro", name
    # an explicit plan/brief model still wins over the configured default
    explicit = SimpleNamespace(run=SimpleNamespace(model_ref={"id": "pro"}), catalog=cat)
    monkeypatch.delenv(DEFAULT_MODEL_ENV, raising=False)
    assert registry.get("coder").resolve_model(explicit) == "pro"


# 5. the prompt/policy text names the CONFIGURED ids and states the policy -------


def _pin_catalog(monkeypatch: pytest.MonkeyPatch, ids: list[str]) -> None:
    """Point the process-wide catalog at exactly these ids (fake provider: no
    key needed), so the policy tests do not read a real ~/.rework/models.json."""
    monkeypatch.setenv(
        "REWORK_MODELS",
        json.dumps([{"id": i, "provider": "fake", "model": i, "tier": 1} for i in ids]),
    )


def test_playbook_text_names_the_configured_ids_and_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rework.orchestrator.agent import RULES, model_policy, orchestrator_tools, render

    _pin_catalog(monkeypatch, ["flash", ESCALATION_MODEL_ID])
    rendered = render(RULES)
    assert "$DEFAULT_MODEL" not in rendered and "$ESCALATION_MODEL" not in rendered
    assert "flash" in rendered and "claude-sonnet-5-5" in rendered
    lowered = rendered.lower()
    assert "escalat" in lowered
    assert "provider error" in lowered and "never" in lowered
    assert "high-risk" in lowered or "high risk" in lowered
    # the spawn_role `model` field's description is the other place the policy
    # lives, and it carries the placeholders until the orchestrator renders it
    spawn = next(t for t in orchestrator_tools() if t.name == "spawn_role")
    field = spawn.input_schema["properties"]["model"]["description"]
    assert "$DEFAULT_MODEL" in field and "$ESCALATION_MODEL" in field
    rendered_field = render(field)
    assert "flash" in rendered_field and "claude-sonnet-5-5" in rendered_field
    assert "$" not in rendered_field  # nothing left unrendered
    # MissionOrchestrator renders the whole tool (description AND schema)
    from rework.orchestrator.agent import render_tool

    tool = render_tool(spawn)
    rendered_field = tool.input_schema["properties"]["model"]["description"]
    assert "flash" in rendered_field and "claude-sonnet-5-5" in rendered_field
    assert "$DEFAULT_MODEL" not in rendered_field

    # changing the env vars changes the rendered text (no code change needed)
    _pin_catalog(monkeypatch, ["cheap-thing", "big-thing"])
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "cheap-thing")
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "big-thing")
    assert model_policy() == ("cheap-thing", "big-thing")
    changed = render(RULES)
    assert "cheap-thing" in changed and "big-thing" in changed
    assert changed != rendered
    assert "flash" not in changed.split("MODEL POLICY")[1].split("\n")[0]


# 6. the MECHANICAL escalation path (needs_model) honours the configured model ---


def _escalation_entry(catalog: ModelCatalog, current_id: str) -> str | None:
    """`MissionRunner.escalation_entry` without a live engine: it reads only the
    catalog and config, so Nones for the rest are honest."""
    from rework.orchestrator.engine import MissionRunner

    runner = MissionRunner(
        "m",
        None,
        None,
        None,
        catalog,
        None,
        Path("."),  # type: ignore[arg-type]
    )
    entry = runner.escalation_entry(current_id)
    return entry.id if entry is not None else None


def test_mechanical_escalation_uses_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry("pro", tier=2),
            _entry(ESCALATION_MODEL_ID, tier=3),
        ]
    )
    # what the old fallback gave: the next tier up within the same provider
    assert cat.promote("flash").id == "pro"
    # the mechanical path escalates to the ONE configured target instead
    assert _escalation_entry(cat, "flash") == ESCALATION_MODEL_ID
    # and it follows the env var, still with no code change
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "pro")
    assert _escalation_entry(cat, "flash") == "pro"


def test_mechanical_escalation_never_names_a_model_the_policy_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy text tells the orchestrator to escalate ONLY to the escalation
    model, so while that model is usable the mechanical path must pick it and
    not a same-provider tier step."""
    cat = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry("mid", tier=2),
            _entry(ESCALATION_MODEL_ID, tier=3),
        ]
    )
    for current in ("flash", "mid"):
        assert _escalation_entry(cat, current) == ESCALATION_MODEL_ID, current
    # the tier step for 'flash' would have been 'mid': the mechanical path must
    # NOT take it while the configured target is usable
    assert cat.promote("flash").id == "mid" != _escalation_entry(cat, "flash")


def test_mechanical_escalation_falls_back_to_the_strongest_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing from the catalog, or keyless on THIS machine: the documented
    fallback is the strongest usable entry, never a bare tier step."""
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "not-in-this-catalog")
    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2)])
    assert cat.strongest_usable().id == "pro"
    assert _escalation_entry(cat, "flash") == "pro"

    keyless = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry("pro", tier=2),
            _entry(ESCALATION_MODEL_ID, tier=3, api_key_env="MISSING_KEY"),
        ]
    )
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    assert keyless.strongest_usable().id == "pro"  # the configured one has no key
    assert _escalation_entry(keyless, "flash") == "pro"


def test_mechanical_escalation_uses_the_strongest_usable_when_the_configured_one_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch that makes the configured-model fallback mechanical.

    `escalation_model_id()` resolves a keyless/missing configured id to the
    strongest USABLE entry, so by the time the engine looks again that id IS in
    the catalog and usable: the only thing left is that it must not be the
    model we are already on. This is the one case the first branch cannot
    answer, and the same-provider tier step must NOT answer it either (here
    there is no higher same-provider tier at all), so a bare promote() would
    hand back nothing while a stronger usable model exists."""
    cat = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry(ESCALATION_MODEL_ID, tier=3),
            _entry("mid", tier=4, provider="anthropic"),  # strongest, other provider
        ]
    )
    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    assert escalation_model_id(cat) == ESCALATION_MODEL_ID  # usable, so no fallback yet
    assert cat.strongest_usable().id == "mid"
    assert cat.promote(ESCALATION_MODEL_ID) is None  # no higher same-provider tier
    assert _escalation_entry(cat, ESCALATION_MODEL_ID) == "mid"


def test_mechanical_escalation_keeps_promote_as_the_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only when the configured target IS the current model and no other usable
    entry is left does the same-provider tier step still apply."""
    cat = ModelCatalog(
        [
            _entry("flash", tier=1),
            _entry(ESCALATION_MODEL_ID, tier=3),
            _entry("huge", tier=5, api_key_env="MISSING_KEY"),
        ]
    )
    monkeypatch.delenv("MISSING_KEY", raising=False)
    assert cat.strongest_usable().id == ESCALATION_MODEL_ID
    assert _escalation_entry(cat, ESCALATION_MODEL_ID) == "huge"


def test_mechanical_escalation_returns_none_on_an_empty_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty catalog must never raise: `_promote_and_resume` reads None as
    "no model to escalate to" and opens the human gate (audited crash:
    `promote()` -> `resolve()` raised UnknownModelError from outside the try)."""
    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    assert _escalation_entry(ModelCatalog([]), "flash") is None


def test_mechanical_escalation_answers_an_unknown_current_id_from_the_strongest_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ORDERED policy: branch 2 answers before promote is ever reached.

    A current id the catalog does not hold (a role whose default_model is
    absent here) is not a special case: the configured escalation id is missing
    too, so `escalation_model_id()` has already resolved to the strongest
    usable entry, which is neither the configured id nor the current one, and
    the engine returns it. promote() - the only step that can raise
    UnknownModelError - is therefore never consulted while a usable entry
    exists. The guard in that last resort only matters with no usable entry at
    all (the empty-catalog regression test above); an unknown or empty current
    id alone does not produce None."""
    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2)])
    assert cat.strongest_usable().id == "pro"
    assert _escalation_entry(cat, "not-in-this-catalog") == "pro"
    assert _escalation_entry(cat, "") == "pro"


# 7. the TUI knob and the orchestrator's own model use the configured default ---


def test_tui_knob_defaults_to_the_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.tui.conversation import default_model

    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2)])
    assert default_model(cat) == "flash"
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "pro")
    assert default_model(cat) == "pro"


def test_cli_and_orchestrator_defaults_use_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rework.cli import default_orchestrator_model

    cat = ModelCatalog([_entry("flash", tier=1), _entry("pro", tier=2)])
    assert default_orchestrator_model(cat) == "flash"
    monkeypatch.setenv(DEFAULT_MODEL_ENV, "pro")
    assert default_orchestrator_model(cat) == "pro"
    from rework.config import default_model_id

    assert default_model_id(cat) == "pro"


# the loopback / key helpers sync-config relies on -------------------------------


def test_is_loopback_url_covers_the_shapes_a_catalog_uses() -> None:
    assert is_loopback_url("http://localhost:20128/v1")
    assert is_loopback_url("http://127.0.0.1:20128/v1")
    assert is_loopback_url("http://[::1]:20128/v1")
    assert is_loopback_url("127.0.0.1:20128")
    assert is_loopback_url("localhost")
    assert not is_loopback_url("https://api.z.ai/api/coding/paas/v4")
    assert not is_loopback_url("http://100.107.125.118:8941/v1")
    assert not is_loopback_url(None) and not is_loopback_url("")


def test_api_key_env_name_matches_the_adapter_lookup() -> None:
    # provider default: no explicit api_key_env, so the provider's env var wins
    assert (
        api_key_env_name(_entry("a", provider="anthropic", api_key_env="")) == "ANTHROPIC_API_KEY"
    )
    assert api_key_env_name(_entry("a", provider="openai", api_key_env="")) == "OPENAI_API_KEY"
    # an explicit api_key_env always wins over the provider default
    assert api_key_env_name(_entry("a", api_key_env="REWORK_ROUTER_KEY")) == "REWORK_ROUTER_KEY"
    # a provider that needs no key, and a provider with no known default
    assert api_key_env_name(ModelEntry(id="f", provider="fake", model="f")) == ""
    assert api_key_env_name(_entry("a", provider="mystery", api_key_env="")) == ""
