"""The configurable default/escalation model, end to end at the unit level.

Hermetic: fake catalogs, monkeypatched env, no network and no real HOME. The
mission's point is that NO default path hardcodes a model id, so these tests
assert both what resolves today and that changing the env vars changes it.
"""

from __future__ import annotations

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
    cat = ModelCatalog([_entry("flash", tier=1), _entry("opus", tier=3)])
    assert escalation_model_id(cat) == ESCALATION_MODEL_ID == "claude-sonnet-5-5"
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "opus")
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
    assert version_key("flash") == () and version_key("pro") == ()
    cat = ModelCatalog([_entry("pro", tier=2), _entry("flash", tier=2)])
    assert cat.strongest_usable().id in {"pro", "flash"}  # total order, no raise
    assert cat.cheapest_usable().id in {"pro", "flash"}


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


def test_playbook_text_names_the_configured_ids_and_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rework.orchestrator.agent import RULES, model_policy, orchestrator_tools, render

    rendered = render(RULES)
    assert "$DEFAULT_MODEL" not in rendered and "$ESCALATION_MODEL" not in rendered
    assert "flash" in rendered and "claude-sonnet-5-5" in rendered
    lowered = rendered.lower()
    assert "escalat" in lowered
    assert "provider error" in lowered and "never" in lowered
    assert "high-risk" in lowered or "high risk" in lowered
    # the spawn_role model description names the same ids
    spawn = next(t for t in orchestrator_tools() if t.name == "spawn_role")
    assert "$DEFAULT_MODEL" in spawn.description  # the placeholder is the knob
    described = render(spawn.description)
    assert "flash" in described and "claude-sonnet-5-5" in described

    monkeypatch.setenv(DEFAULT_MODEL_ENV, "cheap-thing")
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "big-thing")
    assert model_policy() == ("cheap-thing", "big-thing")
    changed = render(RULES)
    assert "cheap-thing" in changed and "big-thing" in changed
    assert changed != rendered


# 6. the TUI knob and the orchestrator's own model use the configured default ---


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
    assert api_key_env_name(_entry("a", provider="anthropic")) == "ANTHROPIC_API_KEY"
    assert api_key_env_name(_entry("a", provider="openai")) == "OPENAI_API_KEY"
    assert api_key_env_name(_entry("a", api_key_env="REWORK_ROUTER_KEY")) == "REWORK_ROUTER_KEY"
    assert api_key_env_name(ModelEntry(id="f", provider="fake", model="f")) == ""
