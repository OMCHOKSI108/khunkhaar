"""Execution budgets shared by every Phase 2 component.

The spec requires hard ceilings on agent steps, browser sessions, API requests and wall-clock
time. Nothing here talks to a target; it only counts, so it can be unit-tested directly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a run tries to exceed one of its configured limits."""

    def __init__(self, kind: str, used: int | float, limit: int | float):
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"{kind} budget exceeded: {used} > {limit}")


@dataclass
class Budget:
    max_steps: int = 80
    max_requests: int = 200
    max_browser_sessions: int = 3
    max_seconds: float = 900.0
    max_concurrency: int = 5

    steps: int = 0
    requests: int = 0
    browser_sessions: int = 0
    started_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _check_cancelled(self) -> None:
        if self.cancelled:
            raise BudgetExceeded("cancelled", 1, 0)

    def consume_steps(self, count: int = 1) -> None:
        with self._lock:
            self._check_cancelled()
            if self.steps + count > self.max_steps:
                raise BudgetExceeded("steps", self.steps + count, self.max_steps)
            self.steps += count

    def consume_requests(self, count: int = 1) -> None:
        with self._lock:
            self._check_cancelled()
            if self.requests + count > self.max_requests:
                raise BudgetExceeded("requests", self.requests + count, self.max_requests)
            self.requests += count

    def open_browser_session(self) -> None:
        with self._lock:
            self._check_cancelled()
            if self.browser_sessions + 1 > self.max_browser_sessions:
                raise BudgetExceeded("browser_sessions", self.browser_sessions + 1,
                                     self.max_browser_sessions)
            self.browser_sessions += 1
            self._check_time_locked()

    def check_time(self) -> None:
        with self._lock:
            self._check_time_locked()

    def _check_time_locked(self) -> None:
        elapsed = time.monotonic() - self.started_at
        if elapsed > self.max_seconds:
            raise BudgetExceeded("seconds", round(elapsed, 1), self.max_seconds)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True

    def can_continue(self) -> bool:
        """Non-raising pre-check so the orchestrator can stop cleanly between missions."""
        if self.cancelled:
            return False
        if time.monotonic() - self.started_at > self.max_seconds:
            return False
        if self.max_requests and self.requests >= self.max_requests:
            return False
        if self.max_steps and self.steps >= self.max_steps:
            return False
        return True

    @property
    def elapsed_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 2)

    def snapshot(self) -> dict:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "requests": self.requests,
            "max_requests": self.max_requests,
            "browser_sessions": self.browser_sessions,
            "max_browser_sessions": self.max_browser_sessions,
            "elapsed_seconds": self.elapsed_seconds,
            "max_seconds": self.max_seconds,
            "cancelled": self.cancelled,
        }
