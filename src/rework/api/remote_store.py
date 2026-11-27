"""The store's READ side over the daemon API (ADR 0021 phase 3c).

A front end's detail views (an open mission's runs, gates, events and spend,
the Cost tab, a chat transcript) read through `StateStore` methods. With a
daemon configured, those calls go to `POST /v1/read` instead of a database
connection: the daemon runs the same method on its own store and returns the
result, encoded and decoded by the method's return annotation, so the client
gets the same typed records a local store would give it.

Only methods in READS are served. They are read-only by construction (no
create/update/append/decide/charge), so the route cannot be used to write:
writes stay explicit commands with their own routes and rules.
"""

from __future__ import annotations

import typing
from functools import cache
from typing import Any

from pydantic import TypeAdapter

# every StateStore method a front end may call remotely; read-only, all of them
READS = frozenset(
    {
        "get_mission",
        "get_run",
        "get_project",
        "get_chat",
        "list_runs",
        "list_gates",
        "list_open_gates",
        "list_projects",
        "list_chats",
        "list_missions",
        "list_missions_light",
        "events_since",
        "mission_spend",
        "run_charges",
        "spend_by_model",
        "spend_history",
        "chat_messages",
        "chat_messages_max_id",
        "chat_messages_since",
        "chat_ids_with_conversation",
        "chat_ids_with_messages",
    }
)


class NotARead(ValueError):
    """The method is not a served read (unknown, or a write)."""


@cache
def adapter(method: str) -> TypeAdapter[Any]:
    """The TypeAdapter for a read's return type (both ends use the same one)."""
    from rework.kernel.state.store import StateStore

    if method not in READS:
        raise NotARead(f"{method} is not a remote read")
    hints = typing.get_type_hints(getattr(StateStore, method))
    return TypeAdapter(hints["return"])


@cache
def _params(method: str) -> dict[str, TypeAdapter[Any]]:
    """One TypeAdapter per keyword parameter, from the method's signature."""
    from rework.kernel.state.store import StateStore

    if method not in READS:
        raise NotARead(f"{method} is not a remote read")
    hints = typing.get_type_hints(getattr(StateStore, method))
    return {name: TypeAdapter(hint) for name, hint in hints.items() if name != "return"}


def bind(method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Client side: positional + keyword arguments as one JSON-ready dict."""
    import inspect

    from rework.kernel.state.store import StateStore

    if method not in READS:
        raise NotARead(f"{method} is not a remote read")
    signature = inspect.signature(getattr(StateStore, method))
    bound = signature.bind(None, *args, **kwargs)  # None stands in for self
    params = _params(method)
    return {
        name: params[name].dump_python(value, mode="json")
        for name, value in list(bound.arguments.items())[1:]
    }


def unbind(method: str, data: dict[str, Any]) -> dict[str, Any]:
    """Server side: typed keyword arguments; an unknown name is refused."""
    params = _params(method)
    unknown = set(data) - set(params)
    if unknown:
        raise ValueError(f"{method} has no parameter {sorted(unknown)[0]}")
    return {name: params[name].validate_python(value) for name, value in data.items()}


def encode(method: str, value: Any) -> Any:
    return adapter(method).dump_python(value, mode="json")


def decode(method: str, data: Any) -> Any:
    value = adapter(method).validate_python(data)
    if method == "list_missions_light":
        # `light` is never serialized (exclude=True): restore what the store
        # says about these rows, so no view mistakes them for full ones
        for mission in value:
            mission.light = True
    return value


class RemoteReadStore:
    """Duck-typed StateStore whose READS run on the daemon. Anything else is a
    loud error, never a silent no-op: a write that reaches here is a bug in the
    caller (it must use a command route)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in READS:
            raise AttributeError(f"{name} is not available on the daemon's read API")

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await self._client.read(name, *args, **kwargs)

        return call

    async def aclose(self) -> None:
        await self._client.aclose()
