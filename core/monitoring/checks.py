"""The three monitoring checks: smoke, regression and invariants.

A check can fail for two very different reasons, and they are never merged. A `failed` check
means the application answered and a stored expectation did not hold. `environment_unavailable`
means nothing could be concluded: the target was down, a test could not run, or the budget ran
out. Only the first becomes a defect-shaped alert, and even then monitoring never creates or
confirms a finding - independent verification remains the only path to that status.
"""

from __future__ import annotations

from typing import Any

from core.execution.api import ApiClient
from core.findings import store as findings_store
from core.metadata import store
from core.notify import notifier
from core.regression import runner
from core.shared import evidence

_SEVERITIES = set(notifier.SEVERITIES)


def run_checks(project_id: str, schedule: dict[str, Any], artifact: dict[str, Any], *,
               target: str, budget, config: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    timeout = float(config.get("check_timeout_seconds", 120))
    for name in schedule.get("checks") or []:
        try:
            if name == "smoke":
                results.append(_smoke(project_id, target, timeout=timeout, budget=budget,
                                      config=config))
            elif name == "regression":
                results.append(_regression(project_id, target, artifact, timeout=timeout,
                                           budget=budget))
            elif name == "invariants":
                results.append(_invariants(project_id, target, artifact, timeout=timeout,
                                           budget=budget))
            else:  # stored rows were validated, so this is a corrupt or hand-edited row
                results.append({"check": name, "status": "error",
                                "note": "unknown check in the stored schedule", "alerts": []})
        except Exception as exc:  # one broken check must not hide the others
            results.append(_internal_error(project_id, name, exc))
    return {
        "checks": results,
        "status": aggregate_status(results),
        "alerts": [alert for result in results for alert in result.get("alerts") or []],
    }


def aggregate_status(results: list[dict[str, Any]]) -> str:
    statuses = [result.get("status") for result in results]
    if "failed" in statuses:
        return "failed"
    if "error" in statuses:
        return "error"
    if "environment_unavailable" in statuses:
        return "environment_unavailable"
    if "passed" in statuses:
        return "passed"
    return "skipped"


def _internal_error(project_id: str, name: str, exc: Exception) -> dict[str, Any]:
    alert = _alert(project_id, "monitoring_blocked", "medium",
                   f"monitoring check '{name}' did not complete: {type(exc).__name__}; no defect "
                   f"is claimed", detail={"check": name, "note": type(exc).__name__})
    return {"check": name, "status": "error",
            "note": f"{type(exc).__name__}; no conclusion about the application was drawn",
            "alerts": [_alert_ref(alert)]}


def _alert(project_id: str, kind: str, severity: str, message: str, *,
           finding_id: str | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    if severity not in _SEVERITIES:
        severity = "high"
    return notifier.notify(project_id, kind, severity, message, finding_id=finding_id,
                           detail=detail)


def _alert_ref(alert: dict[str, Any]) -> dict[str, Any]:
    return {"id": alert.get("id"), "kind": alert.get("kind"),
            "severity": alert.get("severity"), "status": alert.get("status")}


# ------------------------------------------------------------------------- smoke


def _smoke(project_id: str, target: str, *, timeout: float, budget, config: dict[str, Any]
           ) -> dict[str, Any]:
    client = ApiClient(target, budget=budget, timeout=min(timeout, 30.0))
    try:
        response = client.get("/")
    finally:
        client.close()
    if response.error is not None:
        alert = _alert(
            project_id, "environment_unavailable", "medium",
            "smoke check could not reach the target; no defect is claimed",
            detail={"check": "smoke", "result": "transport", "note": response.error_kind or ""},
        )
        return {"check": "smoke", "status": "environment_unavailable",
                "note": f"transport error ({response.error_kind or 'unknown'}); the target did "
                        f"not answer", "http_status": None, "alerts": [_alert_ref(alert)]}
    assert response.status is not None
    if response.status >= 500:
        alert = _alert(
            project_id, "smoke_failed", "high",
            f"smoke check received HTTP {response.status} from the target",
            detail={"check": "smoke", "http_status": response.status, "result": "failed"},
        )
        return {"check": "smoke", "status": "failed",
                "note": f"the target answered HTTP {response.status}",
                "http_status": response.status, "alerts": [_alert_ref(alert)]}
    return {"check": "smoke", "status": "passed",
            "note": f"the target answered HTTP {response.status}", "http_status": response.status,
            "alerts": []}


# -------------------------------------------------------------- regression / invariants


def _regression(project_id: str, target: str, artifact: dict[str, Any], *, timeout: float,
                budget) -> dict[str, Any]:
    selection = runner.select_all(project_id)
    if not selection["selected"]:
        return {"check": "regression", "status": "skipped",
                "note": "no runnable regression test exists; nothing was verified",
                "alerts": []}
    return _execute(project_id, "regression", "regression_failed", selection, target,
                    artifact=artifact, timeout=timeout, budget=budget)


def _invariants(project_id: str, target: str, artifact: dict[str, Any], *, timeout: float,
                budget) -> dict[str, Any]:
    invariants = findings_store.list_invariants(project_id)
    if not invariants:
        return {"check": "invariants", "status": "skipped",
                "note": "no documented invariant exists; nothing was verified", "alerts": []}
    findings = findings_store.list_findings(project_id)
    linked = {str(item["id"]): item for item in findings if item.get("invariant_id")}
    tests = [test for test in store.list_regression_tests(project_id)
             if str(test.get("finding_id")) in linked
             and test.get("reliability") != "quarantined"]
    covered_invariants = {str(linked[str(test.get("finding_id"))]["invariant_id"])
                          for test in tests if str(test.get("finding_id")) in linked}
    uncovered = [invariant for invariant in invariants
                 if str(invariant.get("id")) not in covered_invariants]
    alerts: list[dict[str, Any]] = []
    if uncovered:
        alert = _alert(
            project_id, "invariant_uncovered", "low",
            f"{len(uncovered)} invariant(s) have no regression test; monitoring cannot verify them",
            detail={"check": "invariants", "count": len(uncovered), "result": "uncovered"},
        )
        alerts.append(_alert_ref(alert))
    if not tests:
        return {"check": "invariants", "status": "skipped",
                "note": f"{len(invariants)} invariant(s) exist but none has a regression test; "
                        f"nothing was verified",
                "uncovered": len(uncovered), "alerts": alerts}
    selection = {"selected": [
        {"test": test, "reason": "the linked finding is attached to a documented invariant"}
        for test in tests], "excluded": [], "changed_paths": []}
    result = _execute(project_id, "invariants", "invariant_failed", selection, target,
                      artifact=artifact, timeout=timeout, budget=budget)
    result["uncovered"] = len(uncovered)
    result["alerts"] = alerts + result.get("alerts", [])
    return result


def _execute(project_id: str, check: str, failure_kind: str, selection: dict[str, Any],
             target: str, *, artifact: dict[str, Any], timeout: float, budget) -> dict[str, Any]:
    execution = runner.execute(
        project_id, selection, target=target,
        users_path=artifact.get("users_file"), db_path=artifact.get("db_path"),
        timeout=timeout, run_id=evidence.new_run_id(f"monitor-{check}"), budget=budget)
    counts = execution["summary"]["by_result"]
    results = execution["results"]
    findings = {str(item["id"]): item for item in findings_store.list_findings(project_id)}
    alerts: list[dict[str, Any]] = []

    for item in results:
        if item.get("result") != "fail":
            continue
        finding = findings.get(str(item.get("finding_id"))) or {}
        title = finding.get("title") or "the recorded defect"
        severity = str(finding.get("severity") or "high")
        alert = _alert(
            project_id, failure_kind, severity,
            f"{check} test {item.get('test_id')} still reproduces finding "
            f"{item.get('finding_id')}: {title}",
            finding_id=str(item.get("finding_id") or "") or None,
            detail={"check": check, "test_id": item.get("test_id"),
                    "finding_id": item.get("finding_id"), "feature": finding.get("feature"),
                    "title": title, "severity": severity, "evidence": item.get("evidence"),
                    "result": "fail"},
        )
        alerts.append(_alert_ref(alert))

    unresolved = int(counts.get("environment_unavailable", 0)) + int(counts.get("not_run", 0))
    if unresolved:
        alert = _alert(
            project_id, "environment_unavailable", "medium",
            f"{unresolved} {check} check(s) could not run; no defect is claimed",
            detail={"check": check, "count": unresolved, "result": "environment_unavailable"},
        )
        alerts.append(_alert_ref(alert))

    if counts.get("fail"):
        status = "failed"
    elif unresolved:
        status = "environment_unavailable"
    elif counts.get("pass"):
        status = "passed"
    else:
        status = "skipped"
    note = (f"{counts.get('pass', 0)} pass, {counts.get('fail', 0)} fail, "
            f"{counts.get('environment_unavailable', 0)} environment_unavailable, "
            f"{counts.get('not_run', 0)} not_run")
    return {"check": check, "status": status, "note": note, "counts": counts,
            "run_id": execution["run_id"], "target_host": execution.get("target_host"),
            "alerts": alerts}
