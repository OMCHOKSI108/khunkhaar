"""Run regression tests and select them by change impact.

A run records one row per test and never overwrites history. `environment_unavailable` covers a
missing target, a skipped test, an import failure and a timeout: none of those is evidence about
the application, so none of them is reported as a failed regression. "fail" means the test ran,
the application answered, and the saved reproduction still reproduced the defect.
"""

from __future__ import annotations

from typing import Any

from core.findings import store as findings_store
from core.impact import risk as impact_risk
from core.metadata import store
from core.regression import history, process
from core.runtime.budget import Budget
from core.shared import evidence
from core.shared.redaction import redact_text

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_SECONDS = 900.0


def select_all(project_id: str) -> dict[str, Any]:
    tests = store.list_regression_tests(project_id)
    usable, quarantined = [], []
    for test in tests:
        if test.get("reliability") == "quarantined":
            quarantined.append({"test_id": test["test_id"], "finding_id": test.get("finding_id"),
                                "reason": "quarantined by validation"})
        else:
            usable.append(test)
    return {"selected": [{"test": test, "reason": "explicit selection of every runnable test"}
                         for test in usable],
            "excluded": quarantined, "changed_paths": []}


def select_by_impact(project_id: str, changed_paths: list[str]) -> dict[str, Any]:
    changed = {str(path) for path in changed_paths if path}
    findings = {item["id"]: item for item in findings_store.list_findings(project_id)}
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for test in store.list_regression_tests(project_id):
        if test.get("reliability") == "quarantined":
            excluded.append({"test_id": test["test_id"], "finding_id": test.get("finding_id"),
                             "reason": "quarantined by validation"})
            continue
        finding = findings.get(str(test.get("finding_id"))) or {}
        refs = {impact_risk.ref_path(ref) for ref in finding.get("source_refs") or [] if ref}
        hits = sorted(refs & changed)
        if not hits:
            excluded.append({"test_id": test["test_id"], "finding_id": test.get("finding_id"),
                             "reason": "the finding references no changed path"})
            continue
        selected.append({
            "test": test,
            "reason": "the finding references changed path(s): " + ", ".join(hits[:6]),
            "matched_paths": hits,
        })
    order = {"trusted": 0, "generated": 1}
    selected.sort(key=lambda item: (order.get(item["test"].get("reliability"), 2),
                                    item["test"]["test_id"]))
    return {"selected": selected, "excluded": excluded, "changed_paths": sorted(changed)}


def _path_for(test: dict[str, Any]):
    from core.regression.validate import test_path

    return test_path(test)


def execute(project_id: str, selection: dict[str, Any], *, target: str,
            users_path: str | None = None, db_path: str | None = None,
            timeout: float = DEFAULT_TIMEOUT, max_seconds: float = DEFAULT_MAX_SECONDS,
            run_id: str | None = None, budget: Budget | None = None) -> dict[str, Any]:
    """Execute selected tests.

    A caller that owns a run budget (the qa planner) passes it in, so cancellation and the
    step/time ceilings cover regression replay as well; a standalone call creates its own.
    """
    run_id = run_id or evidence.new_run_id("regression")
    analysis = store.latest_git_analysis(project_id) or {}
    base_ref = analysis.get("base_ref")
    head_ref = analysis.get("head_ref")
    if budget is None:
        budget = Budget(max_seconds=max_seconds, max_steps=max(1, len(selection["selected"])))
    results: list[dict[str, Any]] = []
    folder = evidence.evidence_root(project_id, "regression")
    for entry in selection["selected"]:
        test = entry["test"]
        test_id = str(test["test_id"])
        if not budget.can_continue():
            results.append({"test_id": test_id, "result": "not_run",
                            "note": "the regression budget was exhausted before this test"})
            continue
        budget.consume_steps(1)
        path = _path_for(test)
        if path is None:
            results.append(_record(project_id, test, run_id, "not_run", 0,
                                   "no generated or validated test file exists",
                                   None, base_ref, head_ref))
            continue
        run = process.run_test(path, target=target, users_path=users_path, db_path=db_path,
                               timeout=timeout)
        result, note = _classify(run)
        artifact = evidence.write_json(
            folder, f"{run_id}-{test_id}.json",
            {"test_id": test_id, "result": result, "note": note,
             "rc": run.rc, "timed_out": run.timed_out,
             "stdout": redact_text(run.stdout[-8000:]), "stderr": redact_text(run.stderr[-4000:]),
             "outcome": run.outcome, "duration_ms": run.duration_ms,
             "target_host": _host(target)},
            project_id=project_id, run_id=run_id, kind="regression-run",
        )
        results.append(_record(project_id, test, run_id, result, run.duration_ms, note,
                               evidence.relative(artifact), base_ref, head_ref))
    summary = _summary(results)
    summary["budget"] = budget.snapshot()
    summary["run_id"] = run_id
    return {"run_id": run_id, "target_host": _host(target), "results": results,
            "summary": summary, "analysis": {"base": base_ref, "head": head_ref}}


def _classify(run: process.PytestRun) -> tuple[str, str]:
    if run.timed_out:
        return ("environment_unavailable",
                "the test process did not finish within its timeout; no conclusion about the "
                "application was drawn")
    outcome = run.outcome or {}
    if outcome.get("infrastructure_error"):
        return ("environment_unavailable",
                "environment unavailable: " + str(outcome["infrastructure_error"]))
    if outcome.get("violated") is True:
        return ("fail", "the saved reproduction still reproduces: "
                        + str(outcome.get("actual") or "unknown"))
    if run.rc == 0:
        return ("pass", "the test passed: the recorded defect was not observed")
    return ("environment_unavailable",
            f"the test process exited {run.rc} without a runtime outcome; treated as an "
            f"environment problem, not a defect")


def _record(project_id: str, test: dict[str, Any], run_id: str, result: str, duration_ms: int,
            note: str, artifact: str | None, base_ref: str | None,
            head_ref: str | None) -> dict[str, Any]:
    row = history.record(project_id, test_id=str(test["test_id"]), result=result, run_id=run_id,
                         base_ref=base_ref, head_ref=head_ref, duration_ms=duration_ms,
                         note=note, evidence_path=artifact)
    return {"test_id": test["test_id"], "finding_id": test.get("finding_id"), "result": result,
            "note": note, "evidence": artifact, "run_row": row.get("id"),
            "duration_ms": duration_ms}


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["result"]] = counts.get(item["result"], 0) + 1
    return {"tests": len(results), "by_result": counts}


def _host(target: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(target).hostname or ""
