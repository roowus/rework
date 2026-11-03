"""Budget ledger — accounting exact, enforcement approximate.

Parallel role calls charge concurrently, so a soft cap can be overshot between
check and charge; that is accepted by design (docs/design.md#kernel). The guard
raises at the next check, and the single persisted writer (the store's charges
table) keeps accounting exact.
"""

from __future__ import annotations

from rework.kernel.state.records import Budget, BudgetState, Charge


def apply_charge(state: BudgetState, charge: Charge) -> BudgetState:
    """Pure fold: returns the new state (does not enforce)."""
    return BudgetState(
        spent_usd=state.spent_usd + charge.cost_usd,
        input_tokens=state.input_tokens + charge.input_tokens,
        output_tokens=state.output_tokens + charge.output_tokens,
    )


def guard(state: BudgetState, budget: Budget) -> None:
    """Raise BudgetExceeded if the state is past any cap. Call before model calls."""
    from rework.kernel.state.records import BudgetExceeded

    if budget.max_usd is not None and state.spent_usd > budget.max_usd:
        raise BudgetExceeded(state, budget)
    if budget.max_input_tokens is not None and state.input_tokens > budget.max_input_tokens:
        raise BudgetExceeded(state, budget)
    if budget.max_output_tokens is not None and state.output_tokens > budget.max_output_tokens:
        raise BudgetExceeded(state, budget)
