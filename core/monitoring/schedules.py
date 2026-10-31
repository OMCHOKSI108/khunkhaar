"""Durable monitoring schedules and their honest due state.

The SQLite row is the queryable summary; the 0600 JSON artifact under `monitoring/schedules/` is
the full definition, including options that have no column (credential-file and database paths).
When the two disagree, the schedule is refused rather than guessed: `monitor set` is the only
writer, and an edited artifact must not change what a cron invocation executes.

Nothing here installs a cron entry or a systemd unit. `doctor_lines` only prints commands the
operator may add to their own scheduler.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.metadata import store
from core.monitoring import cron
from core.shared import paths
from core.shared.redaction import redact_obj

MODES = ("observe", "controlled")
CHECKS = ("smoke", "regression", "invariants")
ARTIFACT_SCHEMA = 1


class ScheduleError(ValueError):
    """A schedule that cannot be stored or whose artifact no longer matches the store."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_timezone(moment: datetime | None = None) -> timezone:
    local = (moment or datetime.now(timezone.utc)).astimezone()
    offset = local.utcoffset() or timedelta(0)
    return timezone(offset, local.tzname() or "local")


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def schedule_timezone(schedule: dict[str, Any]) -> timezone:
    offset = schedule.get("timezone_offset_seconds")
    if offset is None:
        artifact, _ = read_artifact(schedule)
        offset = (artifact or {}).get("timezone_offset_seconds", 0)
    return timezone(timedelta(seconds=int(offset or 0)),
                    str(schedule.get("timezone_name") or "local"))


def schedule_id_for(project_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{project_id}|{name}".encode("utf-8")).hexdigest()[:10]
    return f"MS-{digest}"


def artifact_path(schedule_id: str) -> Path:
    return paths.monitoring_dir("schedules") / f"{schedule_id}.json"


def create_or_update(project_id: str, name: str, expression: str, mode: str = "controlled",
                     target: str | None = None, checks: list[str] | None = None, *,
                     users_file: str | None = None, db_path: str | None = None,
                     now: datetime | None = None,
                     tz: timezone | None = None) -> tuple[dict[str, Any], bool]:
    if not store.get_project(project_id):
        raise ScheduleError(f"project '{project_id}' is not registered")
    label = (name or "").strip()
    if not label or len(label) > 80:
        raise ScheduleError("a schedule needs a name of 1-80 characters")
    if mode not in MODES:
        raise ScheduleError(f"mode must be one of {', '.join(MODES)}; monitoring checks are "
                            f"read-only")
    chosen = [item.strip().lower() for item in (checks or []) if item and item.strip()]
    if not chosen:
        raise ScheduleError(f"choose at least one check: {', '.join(CHECKS)}")
    unknown = sorted(set(chosen) - set(CHECKS))
    if unknown:
        raise ScheduleError(f"unknown check(s): {', '.join(unknown)}; choose from "
                            f"{', '.join(CHECKS)}")
    if not target:
        raise ScheduleError("a schedule needs a target; every monitoring check sends requests")
    parsed = cron.CronExpression.parse(expression)

    moment = (now or utcnow()).astimezone(timezone.utc)
    zone = tz or local_timezone(moment)
    next_due = parsed.next_after(moment.astimezone(zone))

    schedule_id = schedule_id_for(project_id, label)
    existing = store.get_monitoring_schedule(schedule_id)
    created = existing is None
    if existing is not None:
        # Re-setting a schedule without --users/--db must not erase the paths a check relies on.
        prior, _ = read_artifact(existing)
        if prior:
            users_file = users_file if users_file is not None else prior.get("users_file")
            db_path = db_path if db_path is not None else prior.get("db_path")
    created_at = (existing or {}).get("created_at") or stamp(moment)
    artifact = artifact_path(schedule_id)
    record = {
        "schedule_id": schedule_id,
        "project_id": project_id,
        "name": label,
        "expression": parsed.expression,
        "mode": mode,
        "target": target.rstrip("/"),
        "checks": chosen,
        "enabled": bool((existing or {}).get("enabled", True)),
        "created_at": created_at,
        "paused_at": (existing or {}).get("paused_at"),
        "last_due_at": (existing or {}).get("last_due_at"),
        "last_run_at": (existing or {}).get("last_run_at"),
        "next_due_at": stamp(next_due),
        "artifact": relative(artifact),
    }
    if not record["enabled"]:
        # A paused schedule keeps its pause; re-setting it does not silently resume checks.
        record["next_due_at"] = (existing or {}).get("next_due_at")
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "schedule_id": schedule_id,
        "project_id": project_id,
        "name": label,
        "expression": parsed.expression,
        "mode": mode,
        "target": record["target"],
        "checks": chosen,
        "users_file": users_file,
        "db_path": db_path,
        "timezone_name": zone.tzname(None),
        "timezone_offset_seconds": int((zone.utcoffset(None) or timedelta(0)).total_seconds()),
        "enabled": record["enabled"],
        "paused_at": record["paused_at"],
        "created_at": created_at,
        "updated_at": stamp(moment),
        "next_due_at": record["next_due_at"],
        "note": ("KHUNKHAAR installs no cron entry and no service. This file is the definition; "
                 "the operator's own scheduler invokes `khunkhaar monitor run-due`. A window "
                 "that passed while the machine was off is recorded as missed, never as a run."),
    }
    _write_artifact(artifact, payload)
    row = store.upsert_monitoring_schedule(record)
    return {**row, **{key: payload[key] for key in
                      ("timezone_name", "timezone_offset_seconds", "users_file", "db_path")}}, created


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_obj(payload), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    path.chmod(0o600)


def relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(paths.kh_root().resolve()))
    except ValueError:
        return str(path)


def read_artifact(schedule: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path = paths.kh_root() / str(schedule.get("artifact") or "")
    allowed = paths.monitoring_dir("schedules").resolve()
    try:
        inside = path.resolve().is_relative_to(allowed)
    except (OSError, ValueError):
        inside = False
    if not schedule.get("artifact") or not inside or not path.is_file():
        return None, "the schedule artifact is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the schedule artifact is unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "the schedule artifact is not a JSON object"
    mismatch = _mismatch(schedule, payload)
    if mismatch:
        return None, (f"the schedule artifact disagrees with the store ({mismatch}); run "
                      f"`khunkhaar monitor set` again")
    return payload, None


def _mismatch(schedule: dict[str, Any], payload: dict[str, Any]) -> str | None:
    pairs = (("schedule_id", schedule.get("schedule_id")), ("project_id", schedule.get("project_id")),
             ("expression", schedule.get("expression")), ("mode", schedule.get("mode")),
             ("target", schedule.get("target")), ("checks", schedule.get("checks")))
    for key, stored in pairs:
        if payload.get(key) != stored:
            return key
    return None


def refresh_artifact(schedule: dict[str, Any]) -> dict[str, Any]:
    payload, problem = read_artifact(schedule)
    if payload is None:
        raise ScheduleError(problem or "the schedule artifact is unavailable")
    payload.update({
        "enabled": schedule.get("enabled"),
        "paused_at": schedule.get("paused_at"),
        "next_due_at": schedule.get("next_due_at"),
        "updated_at": stamp(utcnow()),
    })
    _write_artifact(paths.kh_root() / str(schedule["artifact"]), payload)
    return payload


def set_enabled(schedule_id: str, enabled: bool, *, now: datetime | None = None) -> dict[str, Any]:
    schedule = store.get_monitoring_schedule(schedule_id)
    if not schedule:
        raise ScheduleError(f"schedule '{schedule_id}' does not exist")
    moment = (now or utcnow()).astimezone(timezone.utc)
    if enabled:
        payload, problem = read_artifact(schedule)
        if payload is None:
            raise ScheduleError(problem or "the schedule artifact is unavailable")
        zone = timezone(timedelta(seconds=int(payload.get("timezone_offset_seconds") or 0)),
                        str(payload.get("timezone_name") or "local"))
        expression = cron.CronExpression.parse(str(schedule["expression"]))
        next_due = stamp(expression.next_after(moment.astimezone(zone)))
        updated = store.update_monitoring_schedule(schedule_id, enabled=True, paused_at=None,
                                                   next_due_at=next_due)
    else:
        updated = store.update_monitoring_schedule(schedule_id, enabled=False,
                                                   paused_at=stamp(moment))
    assert updated is not None
    refresh_artifact(updated)
    return updated


def advance(schedule_id: str, *, next_due_at: str, last_due_at: str | None = None,
            last_run_at: str | None = None) -> dict[str, Any]:
    schedule = store.update_monitoring_schedule(
        schedule_id, next_due_at=next_due_at,
        last_due_at=last_due_at or schedule_last_due(schedule_id),
        last_run_at=last_run_at or schedule_last_run(schedule_id))
    assert schedule is not None
    refresh_artifact(schedule)
    return schedule


def schedule_last_due(schedule_id: str) -> str | None:
    row = store.get_monitoring_schedule(schedule_id)
    return (row or {}).get("last_due_at")


def schedule_last_run(schedule_id: str) -> str | None:
    row = store.get_monitoring_schedule(schedule_id)
    return (row or {}).get("last_run_at")


def due_windows(schedule: dict[str, Any], now: datetime) -> list[datetime]:
    """Every scheduled minute at or after `next_due_at` up to `now` (inclusive), oldest first."""
    start = parse_stamp(str(schedule.get("next_due_at") or ""))
    moment = now.astimezone(timezone.utc)
    if start is None or moment < start:
        return []
    payload, problem = read_artifact(schedule)
    if payload is None:
        raise ScheduleError(problem or "the schedule artifact is unavailable")
    zone = timezone(timedelta(seconds=int(payload.get("timezone_offset_seconds") or 0)),
                    str(payload.get("timezone_name") or "local"))
    expression = cron.CronExpression.parse(str(schedule["expression"]))
    first = start.astimezone(zone)
    if not expression.matches(first):
        raise ScheduleError("next_due_at is not a scheduled occurrence; run `monitor resume` "
                            "to recalculate it")
    return ([first] + expression.occurrences(first, moment.astimezone(zone), limit=999))[:1000]


def next_future(schedule: dict[str, Any], now: datetime) -> datetime | None:
    payload, problem = read_artifact(schedule)
    if payload is None:
        return None
    zone = timezone(timedelta(seconds=int(payload.get("timezone_offset_seconds") or 0)),
                    str(payload.get("timezone_name") or "local"))
    expression = cron.CronExpression.parse(str(schedule["expression"]))
    return expression.next_after(now.astimezone(zone))


def state(schedule: dict[str, Any], now: datetime, *, grace_seconds: int = 900) -> dict[str, Any]:
    moment = now.astimezone(timezone.utc)
    payload, problem = read_artifact(schedule)
    due = parse_stamp(str(schedule.get("next_due_at") or ""))
    item = {
        "schedule_id": schedule.get("schedule_id"),
        "project_id": schedule.get("project_id"),
        "name": schedule.get("name"),
        "expression": schedule.get("expression"),
        "mode": schedule.get("mode"),
        "target": schedule.get("target"),
        "checks": schedule.get("checks") or [],
        "enabled": bool(schedule.get("enabled")),
        "next_due_at": schedule.get("next_due_at"),
        "last_due_at": schedule.get("last_due_at"),
        "last_run_at": schedule.get("last_run_at"),
        "timezone": (payload or {}).get("timezone_name"),
        "artifact": schedule.get("artifact"),
        "artifact_problem": problem,
    }
    if not schedule.get("enabled"):
        item.update({"state": "paused", "overdue_seconds": 0,
                     "note": "paused by the operator; a pause never creates a missed run"})
        return item
    if payload is None:
        item.update({"state": "misconfigured", "overdue_seconds": 0, "note": problem})
        return item
    if due is None:
        item.update({"state": "misconfigured", "overdue_seconds": 0,
                     "note": "next_due_at is absent; resume the schedule to recalculate it"})
        return item
    overdue = int((moment - due).total_seconds())
    if overdue < 0:
        item.update({"state": "scheduled", "overdue_seconds": 0,
                     "note": f"the next window is in {-overdue} seconds"})
    elif overdue <= grace_seconds:
        item.update({"state": "due", "overdue_seconds": overdue,
                     "note": "the window is due and still inside the grace period"})
    else:
        item.update({"state": "missed", "overdue_seconds": overdue,
                     "note": ("the window passed without a completed run-due; it will be "
                              "recorded as missed, not as a successful check")})
    return item


def doctor_lines(schedule: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    """Exact lines the operator can install themselves; nothing is written to the host."""
    launcher = shutil.which("khunkhaar")
    python = shutil.which("python3") or "python3"
    command = (launcher if launcher
               else f"{python} {paths.kh_root() / 'core' / 'cli.py'}")
    log = paths.logs_dir() / "monitor.log"
    line = (f"cd {paths.kh_root()} && {command} monitor run-due "
            f"--project {schedule.get('project_id')} --schedule {schedule.get('schedule_id')} "
            f">> {log} 2>&1")
    return [
        f"# {schedule.get('name')} ({schedule.get('schedule_id')}); KHUNKHAAR installed nothing",
        f"{schedule.get('expression')} {line}",
        "# or one frequent user timer that executes every due schedule:",
        f"systemd-run --user --on-calendar='*:0/5' --unit=khunkhaar-monitor {line}",
    ]
