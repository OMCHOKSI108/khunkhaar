"""Execute due monitoring windows without ever pretending a check ran.

The machine's own scheduler calls `monitor run-due`. Each call compares the stored next window
with the real clock. Windows that passed while KHUNKHAAR was not running are written as `missed`
rows with no start time, and the next window is advanced before any check starts so a crash
cannot silently replay a window. `catch_up: false` (the default) means a stale window is recorded
as missed, not executed late and reported as on time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.metadata import store
from core.monitoring import checks, schedules
from core.notify import notifier
from core.runtime.budget import Budget
from core.shared import evidence
from core.shared.config import current
from core.shared.target_policy import preflight_target

MAX_MISSED_ROWS = 100


def run_due(*, project_id: str | None = None, schedule_id: str | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    cfg = current()
    monitoring = cfg.section("monitoring") or {}
    moment = (now or schedules.utcnow()).astimezone(timezone.utc)
    base = {"now": schedules.stamp(moment), "project_id": project_id,
            "schedule_id": schedule_id}
    if not monitoring.get("enabled"):
        return {**base, "enabled": False, "status": "disabled", "executed": 0, "missed": 0,
                "schedules": [],
                "note": ("monitoring is disabled in config/khunkhaar.yaml; nothing was due, "
                         "dispatched or recorded")}
    selected = _select(project_id, schedule_id)
    if isinstance(selected, dict):
        return {**base, "enabled": True, **selected}
    results = [_run_schedule(schedule, moment, monitoring) for schedule in selected]
    return {
        **base,
        "enabled": True,
        "status": _aggregate([item.get("status") for item in results]),
        "executed": sum(1 for item in results if item.get("executed")),
        "missed": sum(int(item.get("missed") or 0) for item in results),
        "schedules": results,
    }


def _select(project_id: str | None, schedule_id: str | None) -> list[dict[str, Any]] | dict[str, Any]:
    if schedule_id:
        schedule = store.get_monitoring_schedule(schedule_id)
        if not schedule:
            return {"status": "error", "schedules": [], "executed": 0, "missed": 0,
                    "note": f"schedule '{schedule_id}' does not exist"}
        if project_id and schedule.get("project_id") != project_id:
            return {"status": "error", "schedules": [], "executed": 0, "missed": 0,
                    "note": f"schedule '{schedule_id}' belongs to another project"}
        return [schedule]
    return store.list_monitoring_schedules(project_id)


def _run_schedule(schedule: dict[str, Any], now: datetime,
                  config: dict[str, Any]) -> dict[str, Any]:
    base = {"schedule_id": schedule.get("schedule_id"), "project_id": schedule.get("project_id"),
            "name": schedule.get("name"), "executed": False, "missed": 0}
    if not schedule.get("enabled"):
        return {**base, "status": "paused",
                "note": "paused by the operator; a pause never creates a missed run"}
    payload, problem = schedules.read_artifact(schedule)
    if payload is None:
        return _misconfigured(schedule, problem or "the schedule artifact is unavailable", base)
    try:
        windows = schedules.due_windows(schedule, now)
        next_due = schedules.next_future(schedule, now)
    except schedules.ScheduleError as exc:
        return _misconfigured(schedule, str(exc), base)
    if not windows:
        state = schedules.state(schedule, now,
                                grace_seconds=int(config.get("schedule_grace_seconds", 900)))
        return {**base, "status": "idle" if state["state"] == "scheduled" else state["state"],
                "next_due_at": schedule.get("next_due_at"), "state": state["state"],
                "note": state.get("note")}
    if next_due is None:
        return _misconfigured(schedule, "no future occurrence could be computed", base)

    grace = int(config.get("schedule_grace_seconds", 900))
    catch_up = bool(config.get("catch_up", False))
    max_overdue = max(1, int(config.get("max_overdue_before_missed", 2)))
    latest = windows[-1]
    within_grace = (now - latest).total_seconds() <= grace
    catch_up_allowed = catch_up and len(windows) <= max_overdue
    existing = store.find_monitoring_run(str(schedule["schedule_id"]), schedules.stamp(latest))
    execute_latest = (within_grace or catch_up_allowed) and existing is None
    missed_windows = windows[:-1] if execute_latest else windows
    missed_count = _record_missed(schedule, missed_windows)
    last_processed = latest if execute_latest else windows[-1]
    schedules.advance(
        str(schedule["schedule_id"]), next_due_at=schedules.stamp(next_due),
        last_due_at=schedules.stamp(last_processed),
        last_run_at=None if execute_latest else (schedule.get("last_run_at")),
    )
    if missed_count:
        alert = notifier.notify(
            str(schedule["project_id"]), "monitoring_missed", "low",
            f"{missed_count} scheduled monitoring window(s) were missed while KHUNKHAAR "
            f"was not running", detail={"schedule_id": schedule.get("schedule_id"),
                                         "count": missed_count, "due_at": "older windows"})
        result = {**base, "missed": missed_count,
                  "note": (f"{missed_count} window(s) were recorded as missed; no check ran for "
                           f"them"),
                  "alerts": [{"id": alert.get("id"), "kind": alert.get("kind"),
                              "severity": alert.get("severity"), "status": alert.get("status")}]}
    else:
        result = {**base, "missed": 0}

    if not execute_latest:
        result.setdefault("alerts", [])
        result["next_due_at"] = schedules.stamp(next_due)
        if existing is not None:
            result["status"] = "idle"
            result["note"] = ("this window already has a recorded execution; it was not run twice")
        elif not missed_count:
            result["status"] = "idle"
            result["note"] = ("every overdue window already has a recorded outcome; nothing was "
                              "run again")
        else:
            result["status"] = "missed"
        return result

    execution = _execute(schedule, payload, latest, now, config)
    prior_alerts = result.get("alerts") or []
    result.update(execution)
    result["alerts"] = prior_alerts + list(execution.get("alerts") or [])
    return result


def _misconfigured(schedule: dict[str, Any], reason: str, base: dict[str, Any]) -> dict[str, Any]:
    alert = notifier.notify(
        str(schedule.get("project_id")), "monitoring_blocked", "medium",
        "a monitoring schedule is misconfigured; no check was executed",
        detail={"schedule_id": schedule.get("schedule_id"), "note": reason})
    return {**base, "status": "misconfigured", "note": reason,
            "alerts": [{"id": alert.get("id"), "kind": alert.get("kind"),
                        "severity": alert.get("severity"), "status": alert.get("status")}]}


def _record_missed(schedule: dict[str, Any], windows: list[datetime]) -> int:
    if not windows:
        return 0
    project_id = str(schedule["project_id"])
    schedule_id = str(schedule["schedule_id"])
    rows = windows
    overflow = len(windows) > MAX_MISSED_ROWS
    if overflow:
        rows = [windows[0]]
    created = 0
    for window in rows:
        due_at = schedules.stamp(window)
        if store.find_monitoring_run(schedule_id, due_at):
            continue
        note = ("the window passed while KHUNKHAAR was not running; no check was executed and "
                "nothing is claimed about the application")
        if overflow:
            note = (f"{len(windows)} scheduled windows were missed between {schedules.stamp(windows[0])} "
                    f"and {schedules.stamp(windows[-1])}; individual rows would be excessive, so "
                    f"this row records the whole gap")
        store.record_monitoring_run({
            "schedule_id": schedule_id, "project_id": project_id, "due_at": due_at,
            "started_at": None, "finished_at": None, "status": "missed", "note": note,
            "evidence": {"windows": len(windows), "first_due_at": schedules.stamp(windows[0]),
                         "last_due_at": schedules.stamp(windows[-1])},
        })
        created += 1
    return created


def _execute(schedule: dict[str, Any], artifact: dict[str, Any], due: datetime,
             now: datetime, config: dict[str, Any]) -> dict[str, Any]:
    project_id = str(schedule["project_id"])
    schedule_id = str(schedule["schedule_id"])
    target = str(schedule.get("target") or "")
    run_id = evidence.new_run_id("monitor")
    row = store.record_monitoring_run({
        "schedule_id": schedule_id, "project_id": project_id, "run_id": run_id,
        "due_at": schedules.stamp(due), "started_at": schedules.stamp(now),
        "status": "running", "note": "the due window started executing",
        "evidence": {"window": schedules.stamp(due), "checks": schedule.get("checks") or []},
    })
    refusal = preflight_target(project_id, target)
    if refusal:
        return _finish(row, project_id, schedule_id, "environment_unavailable", refusal, run_id,
                       config, target=None)
    if not store.get_project(project_id):
        return _finish(row, project_id, schedule_id, "environment_unavailable",
                       "the project is no longer registered", run_id, config, target=None)
    budget = Budget(max_steps=500, max_requests=10, max_browser_sessions=0,
                    max_seconds=float(config.get("check_max_seconds", 600)))
    try:
        result = checks.run_checks(project_id, schedule, artifact, target=target, budget=budget,
                                   config=config)
    except Exception as exc:
        return _finish(row, project_id, schedule_id, "error",
                       f"the check runner stopped unexpectedly ({type(exc).__name__}); no "
                       f"conclusion about the application was drawn", run_id, config, target=target)
    status = str(result.get("status") or "error")
    note = "; ".join(f"{item['check']}: {item['status']}" for item in result["checks"])
    return _finish(row, project_id, schedule_id, status, note, run_id, config,
                   target=target, checks_result=result, budget=budget, schedule=schedule)


def _finish(row: dict[str, Any], project_id: str, schedule_id: str, status: str, note: str,
            run_id: str, config: dict[str, Any], *, target: str | None,
            checks_result: dict[str, Any] | None = None, budget=None,
            schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    finished = schedules.utcnow()
    folder = evidence.evidence_root(project_id, "monitoring")
    payload = {
        "run_id": run_id,
        "schedule_id": schedule_id,
        "status": status,
        "note": note,
        "started_at": row.get("started_at"),
        "finished_at": schedules.stamp(finished),
        "target_host": _host(target) if target else None,
        "checks": (checks_result or {}).get("checks") or [],
        "alerts": (checks_result or {}).get("alerts") or [],
        "budget": budget.snapshot() if budget else None,
        "wording": ("a failed check is a monitoring signal, not an independently confirmed "
                    "defect; environment_unavailable is never a defect"),
    }
    artifact = evidence.write_json(folder, f"{run_id}-{schedule_id}.json", payload,
                                   project_id=project_id, run_id=run_id,
                                   kind="monitoring-run")
    evidence_row = {"artifact": evidence.relative(artifact),
                    "alerts": payload["alerts"],
                    "checks": [{"check": item.get("check"), "status": item.get("status")}
                               for item in payload["checks"]]}
    store.update_monitoring_run(int(row["id"]), finished_at=schedules.stamp(finished),
                                status=status, note=note, evidence=evidence_row)
    if status in ("failed", "error", "environment_unavailable"):
        _delivery_alert(project_id, status, note, schedule_id)
    store.update_monitoring_schedule(schedule_id, last_run_at=schedules.stamp(finished))
    updated = store.get_monitoring_schedule(schedule_id)
    if updated:
        try:
            schedules.refresh_artifact(updated)
        except schedules.ScheduleError:
            pass
    return {"executed": True, "status": status, "run_row": row.get("id"), "run_id": run_id,
            "finished_at": schedules.stamp(finished), "note": note,
            "evidence": evidence_row.get("artifact"),
            "checks": payload["checks"], "alerts": payload["alerts"]}


def _delivery_alert(project_id: str, status: str, note: str, schedule_id: str) -> None:
    if status == "failed":
        return
    notifier.notify(
        project_id, "environment_unavailable", "medium",
        "a monitoring run could not draw a conclusion about the application; no defect is claimed",
        detail={"schedule_id": schedule_id, "result": status, "note": note})


def _host(target: str | None) -> str | None:
    if not target:
        return None
    from urllib.parse import urlsplit

    return urlsplit(target).hostname


def _aggregate(statuses: list[str]) -> str:
    order = ("failed", "error", "environment_unavailable", "misconfigured", "missed", "passed",
             "skipped", "idle", "paused")
    for candidate in order:
        if candidate in statuses:
            return candidate
    return "idle"
