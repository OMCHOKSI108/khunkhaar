"""Phase 2 execution budgets must be hard, thread-safe ceilings."""

from __future__ import annotations

import time

import pytest

from core.runtime.budget import Budget, BudgetExceeded


def test_step_and_request_ceilings_raise():
    budget = Budget(max_steps=2, max_requests=1)
    budget.consume_steps(2)
    with pytest.raises(BudgetExceeded) as exc:
        budget.consume_steps()
    assert exc.value.kind == "steps"

    budget.consume_requests(1)
    with pytest.raises(BudgetExceeded) as exc:
        budget.consume_requests(1)
    assert exc.value.kind == "requests"


def test_browser_sessions_are_counted():
    budget = Budget(max_browser_sessions=1)
    budget.open_browser_session()
    with pytest.raises(BudgetExceeded):
        budget.open_browser_session()


def test_cancellation_blocks_further_work():
    budget = Budget()
    budget.cancel()
    assert budget.can_continue() is False
    with pytest.raises(BudgetExceeded):
        budget.consume_steps()
    with pytest.raises(BudgetExceeded):
        budget.open_browser_session()


def test_time_ceiling_uses_monotonic_start():
    budget = Budget(max_seconds=10)
    budget.started_at = time.monotonic() - 11
    with pytest.raises(BudgetExceeded) as exc:
        budget.check_time()
    assert exc.value.kind == "seconds"
    assert budget.can_continue() is False


def test_snapshot_is_json_safe_and_redaction_free():
    budget = Budget(max_steps=5)
    budget.consume_steps()
    snap = budget.snapshot()
    assert snap["steps"] == 1 and snap["max_steps"] == 5
    assert snap["cancelled"] is False
    assert isinstance(snap["elapsed_seconds"], float)
