"""Deterministic store fault injection for integration tests.

`FaultStore` wraps a real StateStore (either backend) and makes chosen calls fail
with `psycopg.OperationalError`, the error a dropped Postgres link raises. Faults are
counted calls, not timers, and nothing touches a socket or sleeps.

    fault = FaultStore(real_store)
    fault.fail_method("charge", times=1, when="after")  # the write lands, the ack is lost
    fault.fail_method("create_run", skip=1)             # the first call passes, the second fails
    fault.fail_on_call(7)                                # the 7th call (retries count) fails
    fault.outage_window(start=3, length=4)               # calls 3..6 fail
    fault.set_down(True)                                 # every call fails until cleared

    fault.set_read_only(True)                            # reads answer, every write is refused

`when="before"` (default): the call never reaches the store, so nothing is written.
`when="after"`: the call runs and lands, then the error is raised. That is the case a
retry must not apply twice.

`fault.error` is the exception factory (default: `psycopg.OperationalError`, a dropped
link). Set it to raise a PERMANENT store error instead, e.g. a read-only or missing-table
sqlite3.OperationalError, to prove the engine does not treat it as an outage.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import psycopg

from rework.kernel.state.resilient import _READS


class FaultStore:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0  # every delegated call, retries included
        self.injected: list[str] = []
        self._by_call: dict[int, str] = {}
        self._by_method: dict[str, list[tuple[int, str]]] = {}  # method -> [(skip, when)]
        self._windows: list[tuple[int, int]] = []
        self._down = False
        self._read_only = False
        self._read_only_after: set[str] = set()
        self.error: Callable[[str], BaseException] = psycopg.OperationalError

    def fail_on_call(self, n: int, *, when: str = "before") -> None:
        self._by_call[n] = when

    def fail_method(
        self, method: str, *, times: int = 1, skip: int = 0, when: str = "before"
    ) -> None:
        """Fail the next `times` calls to `method`, after letting `skip` calls pass."""
        self._by_method.setdefault(method, []).extend([(skip, when)] * times)

    def outage_window(self, start: int, length: int) -> None:
        self._windows.append((start, start + length))

    def set_down(self, down: bool) -> None:
        self._down = down

    def set_read_only(self, on: bool) -> None:
        """Every write is refused; reads still answer (a file that became read-only)."""
        self._read_only = on

    def read_only_after(self, method: str) -> None:
        """The file turns read-only once `method` has landed (a chmod mid-run)."""
        self._read_only_after.add(method)

    def _mode_for(self, method: str, n: int) -> str | None:
        if self._down:
            return "before"
        if self._read_only and method not in _READS:
            return "before"
        if n in self._by_call:
            return self._by_call.pop(n)
        if any(start <= n < end for start, end in self._windows):
            return "before"
        queue = self._by_method.get(method, [])
        if queue:
            skip, when = queue[0]
            if skip > 0:
                queue[0] = (skip - 1, when)
                return None
            queue.pop(0)
            return when
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            mode = self._mode_for(name, self.calls)
            if mode is None:
                result = await attr(*args, **kwargs)
                if name in self._read_only_after:
                    self._read_only = True
                return result
            if mode == "after":
                await attr(*args, **kwargs)  # the write lands; only the ack is lost
            self.injected.append(f"{name}#{self.calls}:{mode}")
            raise self.error(f"injected link drop on {name} (call {self.calls})")

        return call
