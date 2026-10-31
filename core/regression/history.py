"""Execution history for regression tests.

Every run is appended; nothing is overwritten. A pass raises confidence by a small step and a
failure lowers it, while `environment_unavailable` and `not_run` leave it untouched because a
target that was down says nothing about the application. The per-test timestamps make "this was
green on main at commit X" a fact the metadata store can answer.
"""

from __future__ import annotations

from typing import Any

from core.metadata import store


def record(project_id: str, *, test_id: str, result: str, run_id: str | None,
           base_ref: str | None, head_ref: str | None, duration_ms: int, note: str,
           evidence_path: str | None) -> dict[str, Any]:
    return store.record_regression_run(
        project_id, test_id=test_id, result=result, run_id=run_id, base_ref=base_ref,
        head_ref=head_ref, duration_ms=duration_ms, note=note, evidence=evidence_path)


def recent(project_id: str, *, test_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return store.list_regression_runs(project_id, test_id=test_id, limit=limit)


def summary(project_id: str, *, limit: int = 500) -> dict[str, Any]:
    runs = store.list_regression_runs(project_id, limit=limit)
    counts: dict[str, int] = {}
    for run in runs:
        counts[run["result"]] = counts.get(run["result"], 0) + 1
    green = store.last_green_regression_run(project_id)
    return {"runs": len(runs), "by_result": counts,
            "last_green": ({"test_id": green["test_id"], "finished_at": green["finished_at"],
                            "head_ref": green.get("head_ref")}
                           if green else None)}
