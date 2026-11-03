"""Kernel seams: StateStore, EventBus, ModelCatalog, BudgetLedger, GateManager.

Everything else in rework is written against these protocols (M1). Submodules:
state/ (store protocol + sqlite + postgres), events/, llm/ (catalog + adapters),
budget/, gates.
"""
