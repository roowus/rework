"""Transient database errors: recognised and retried in ONE place.

A Postgres deadlock (SQLSTATE 40P01) and a serialization failure (40001) are not
bugs: the server aborted one transaction on purpose so the same statement can be
retried. Found live on 2026-11-14: a mission runner crashed with
`DeadlockDetected` on `UPDATE chats SET updated_at = $1 WHERE id = $2`, the
exception was never classified, and the daemon marked the mission FAILED and
threw its work away.

Everything here is duck-typed (`getattr(exc, "sqlstate", None)`, walking
`__cause__` / `__context__` / `.orig`), so importing this module never requires
psycopg: sqlite-only installs (and the conformance suite) import it too. sqlite's
transient class is its write lock, `sqlite3.OperationalError("database is
locked")`.

Policy (see docs/design.md#reliability):
- MAX_ATTEMPTS total attempts per store operation, then re-raise.
- A short jittered sleep in RETRY_SLEEP_RANGE_S between attempts.
- Exactly one WARNING per retry, naming the attempt number and the SQLSTATE.
- A logical error (unique violation 23505, foreign key 23503, syntax 42601,
  undefined column 42703, ...) is NOT transient: it propagates on attempt 1.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any, overload

log = logging.getLogger("rework.state.retry")

#: SQLSTATEs that mean "the transaction was rolled back, run it again".
#: 40P01 deadlock_detected, 40001 serialization_failure.
TRANSIENT_SQLSTATES = frozenset({"40P01", "40001"})

#: Total attempts per store operation (first try plus two retries).
MAX_ATTEMPTS = 3

#: Sleep bounds in seconds between attempts: short, and jittered so two
#: processes that deadlocked each other do not retry in lock step.
RETRY_SLEEP_RANGE_S = (0.01, 0.1)

# psycopg error type names that carry those SQLSTATEs (a wrapper that dropped
# the attribute still classifies correctly)
_TRANSIENT_TYPE_NAMES = frozenset({"DeadlockDetected", "SerializationFailure"})

# sqlite's write-lock message (the sqlite analogue of a transient abort)
_SQLITE_LOCKED = ("database is locked", "database table is locked")

# How deep we are inside a retried operation. A nested store call (update_run ->
# get_run) does NOT retry on its own: the outer attempt owns the retry budget, so
# a transient error inside one attempt re-runs the whole operation and the log
# carries one warning per retry, not one per nested call.
_DEPTH: ContextVar[int] = ContextVar("rework_transient_db_depth", default=0)


class TransientDatabaseError(RuntimeError):
    """A transient database error that survived MAX_ATTEMPTS.

    It CARRIES the original exception (`raise ... from exc`, so the cause chain
    stays intact) and `is_transient_db_error` returns True for it, which is how
    the daemon's crash handler still recognises the failure as transient and
    requeues the mission from its checkpoint instead of failing it.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.attempts = attempts


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception plus its causes/contexts and psycopg's `.orig` wrapper."""
    queue: list[BaseException] = [exc]
    seen: set[int] = set()
    while queue:
        cur = queue.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for nxt in (cur.__cause__, cur.__context__, getattr(cur, "orig", None)):
            if isinstance(nxt, BaseException) and id(nxt) not in seen:
                queue.append(nxt)


def error_sqlstate(exc: BaseException) -> str | None:
    """First SQLSTATE found in the exception chain, or None."""
    for cur in _chain(exc):
        for holder in (cur, getattr(cur, "diag", None)):
            state = getattr(holder, "sqlstate", None)
            if isinstance(state, str) and state:
                return state
    return None


def _is_sqlite_locked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _SQLITE_LOCKED)


def is_transient_db_error(exc: BaseException) -> bool:
    """True when retrying the same operation can succeed.

    TransientDatabaseError counts (it is what exhaustion raises), as does any
    exception in the chain carrying a transient SQLSTATE, a psycopg deadlock or
    serialization failure, or sqlite reporting its write lock.
    """
    if isinstance(exc, TransientDatabaseError):
        return True
    for cur in _chain(exc):
        state = getattr(cur, "sqlstate", None)
        if isinstance(state, str) and state in TRANSIENT_SQLSTATES:
            return True
        if type(cur).__name__ in _TRANSIENT_TYPE_NAMES:
            return True
        diag = getattr(cur, "diag", None)
        diag_state = getattr(diag, "sqlstate", None)
        if isinstance(diag_state, str) and diag_state in TRANSIENT_SQLSTATES:
            return True
        if _is_sqlite_locked(cur):
            return True
    return False


def transient_error_summary(exc: BaseException) -> str:
    """One line naming the error and its SQLSTATE, for logs and event payloads."""
    state = error_sqlstate(exc)
    name = type(exc).__name__
    return f"{name} (SQLSTATE {state})" if state else f"{name}: {exc}"


async def retry_transient[T](
    operation: Callable[[], Awaitable[T]],
    *,
    cleanup: Callable[[], Awaitable[None]] | None = None,
    label: str = "store operation",
    attempts: int = MAX_ATTEMPTS,
) -> T:
    """Await `operation()`, retrying a transient database error.

    `cleanup` runs after a failed attempt and BEFORE the next one (roll back the
    aborted transaction), so every attempt starts from a clean connection. After
    `attempts` transient failures the original error is re-raised wrapped in
    TransientDatabaseError (cause intact), which the daemon still classifies as
    transient. Any other exception propagates on the first attempt.
    """
    if _DEPTH.get():
        return await operation()  # a nested call: the outer attempt owns the retry
    token = _DEPTH.set(1)
    try:
        for attempt in range(1, attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if not is_transient_db_error(exc):
                    raise
                state = error_sqlstate(exc)
                if attempt >= attempts:
                    log.error(
                        "%s: transient database error persisted after %d attempts: %s: %s",
                        label,
                        attempts,
                        type(exc).__name__,
                        exc,
                    )
                    raise TransientDatabaseError(
                        f"{label} failed with a transient database error after"
                        f" {attempts} attempts: {type(exc).__name__}: {exc}",
                        sqlstate=state,
                        attempts=attempts,
                    ) from exc
                if cleanup is not None:
                    await cleanup()
                delay = random.uniform(*RETRY_SLEEP_RANGE_S)
                log.warning(
                    "%s: transient database error %s (SQLSTATE %s) on attempt %d of %d,"
                    " retrying in %.0f ms",
                    label,
                    type(exc).__name__,
                    state or "unknown",
                    attempt,
                    attempts,
                    delay * 1000,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover
    finally:
        _DEPTH.reset(token)


@overload
def retry_transient_methods[T](cls: type[T], *, cleanup: str | None = None) -> type[T]: ...


@overload
def retry_transient_methods(
    cls: None = None, *, cleanup: str | None = None
) -> Callable[[type[Any]], type[Any]]: ...


def retry_transient_methods(
    cls: type[Any] | None = None, *, cleanup: str | None = None
) -> Callable[[type[Any]], type[Any]] | type[Any]:
    """Class decorator: give every public async method the shared retry.

    SqliteStore has no single wrapper of its own (each method talks to the one
    aiosqlite connection directly), so the class object is its seam. Private
    helpers stay unwrapped: the public method that called them retries the whole
    call. Nested calls share the outer budget (see `_DEPTH`).

    `cleanup` names an optional awaitable no-argument attribute on the instance
    that runs after a failed attempt and BEFORE the retry, exactly like the
    `cleanup` argument of `retry_transient`. The sqlite store passes
    `cleanup="_rollback"`: its retry replays the whole method body, so the work
    the aborted attempt already stored in its transaction must be discarded
    first. It is a NAME, not a callable, because the decorator runs at class
    creation time, when there is no instance to bind to. The default None keeps
    the previous behaviour (no cleanup) for every other user.
    """

    def decorate(target: type[Any]) -> type[Any]:
        for name, value in list(vars(target).items()):
            if name.startswith("_") or not inspect.iscoroutinefunction(value):
                continue
            setattr(target, name, _wrap_method(value, cleanup))
        return target

    return decorate if cls is None else decorate(cls)


def _wrap_method(
    method: Callable[..., Awaitable[Any]], cleanup: str | None = None
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await retry_transient(
            lambda: method(self, *args, **kwargs),
            cleanup=None if cleanup is None else getattr(self, cleanup),
            label=f"{type(self).__name__}.{method.__name__}",
        )

    return wrapper
