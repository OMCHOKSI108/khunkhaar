"""`khunkhaar monitor` - durable schedules executed by the operator's own scheduler.

`set` stores a definition and prints the exact line the operator may add to cron or a systemd
user timer. KHUNKHAAR installs nothing. `run-due` compares stored windows with the real clock:
a window that passed while the machine was off is recorded as `missed`, never as a successful
check. Every check sends requests, so `set` and `run-due` both apply the shared outbound policy
and authorisation preflight before anything is sent.
"""

from __future__ import annotations

import argparse
import json
from datetime import timezone
from pathlib import Path

from core.metadata import store
from core.monitoring import cron, runner, schedules
from core.project_discovery import detector
from core.shared.config import current
from core.shared.target_policy import preflight_target

_EXIT = {"failed": 1, "error": 2, "environment_unavailable": 2, "misconfigured": 2, "missed": 3}


def _json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _resolve_project(prefix: str, project: str | None, path: str | None
                     ) -> tuple[str | None, str | None]:
    if project:
        if store.get_project(project):
            return project, None
        return None, f"khunkhaar {prefix}: project '{project}' is not registered"
    if path:
        root = Path(path).expanduser().resolve()
        project_id = detector.project_id_for(root)
        if project_id and store.get_project(project_id):
            return project_id, None
        return None, (f"khunkhaar {prefix}: {root} is not a registered project. Run "
                      f"`khunkhaar project register --path {root}` first.")
    return None, (f"khunkhaar {prefix}: pass --project <id> (see `khunkhaar projects`) or "
                  f"--path <repository>")


def _enabled(prefix: str) -> str | None:
    if current().get("monitoring", "enabled", False):
        return None
    return (f"khunkhaar {prefix}: monitoring is disabled. Set `monitoring.enabled: true` in "
            f"config/khunkhaar.yaml first; nothing is scheduled or dispatched while it is off.")


def _checks(raw: str) -> list[str]:
    return [item.strip().lower() for item in (raw or "").split(",") if item.strip()]


def set_schedule(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor set")
    parser.add_argument("--project", help="registered project id")
    parser.add_argument("--path", help="registered repository (alternative to --project)")
    parser.add_argument("--name", default="default", help="schedule name (default: default)")
    parser.add_argument("--schedule", required=True,
                        help="five-field cron expression, e.g. \"0 3 * * *\"")
    parser.add_argument("--mode", choices=schedules.MODES, default="controlled")
    parser.add_argument("--target", required=True, help="base URL of the test environment")
    parser.add_argument("--checks", default="smoke",
                        help="comma-separated checks: smoke, regression, invariants")
    parser.add_argument("--users", help="JSON file mapping role -> {username,password}")
    parser.add_argument("--db", help="SQLite database path for persistence probes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    refusal = _enabled("monitor set")
    if refusal:
        print(refusal)
        return 2
    project_id, refusal = _resolve_project("monitor set", args.project, args.path)
    if refusal:
        print(refusal)
        return 2
    refusal = preflight_target(project_id, args.target)
    if refusal:
        print(f"khunkhaar monitor set: {refusal}")
        return 2
    try:
        schedule, created = schedules.create_or_update(
            project_id, args.name, args.schedule, mode=args.mode, target=args.target,
            checks=_checks(args.checks), users_file=args.users, db_path=args.db)
    except (schedules.ScheduleError, cron.CronError) as exc:
        print(f"khunkhaar monitor set: {exc}")
        return 2

    payload = {
        "created": created,
        "schedule": schedule,
        "installed": False,
        "next": (f"nothing was installed. Add `khunkhaar monitor run-due --schedule "
                 f"{schedule['schedule_id']}` to your own scheduler; `khunkhaar monitor doctor "
                 f"--schedule {schedule['schedule_id']}` prints the exact line."),
    }
    if args.json:
        _json(payload)
        return 0
    print(f"{'schedule':<14}{schedule['schedule_id']} ({'created' if created else 'updated'})")
    print(f"{'name':<14}{schedule['name']}")
    print(f"{'expression':<14}{schedule['expression']}")
    print(f"{'target':<14}{schedule['target']}")
    print(f"{'checks':<14}{', '.join(schedule.get('checks') or [])}")
    print(f"{'next due':<14}{schedule.get('next_due_at')} "
          f"({schedule.get('timezone_name') or 'local'})")
    print(f"{'artifact':<14}{schedule.get('artifact')} (0600)")
    print(payload["next"])
    return 0


def doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor doctor")
    parser.add_argument("--project", help="registered project id")
    parser.add_argument("--path", help="registered repository")
    parser.add_argument("--schedule", help="one schedule id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    if args.schedule:
        schedule = store.get_monitoring_schedule(args.schedule)
        if not schedule:
            print(f"khunkhaar monitor doctor: schedule '{args.schedule}' does not exist")
            return 1
        rows = [schedule]
    else:
        project_id = None
        if args.project or args.path:
            project_id, refusal = _resolve_project("monitor doctor", args.project, args.path)
            if refusal:
                print(refusal)
                return 2
        rows = store.list_monitoring_schedules(project_id)
    if not rows:
        print("no monitoring schedules exist. A schedule is created with `khunkhaar monitor set`.")
        return 0

    payload = {"schedules": [], "installed": False,
               "wording": ("KHUNKHAAR installed nothing; these are lines the operator may add to "
                           "their own scheduler")}
    for schedule in rows:
        lines = schedules.doctor_lines(schedule)
        payload["schedules"].append({"schedule_id": schedule.get("schedule_id"),
                                     "name": schedule.get("name"), "lines": lines})
        if not args.json:
            for line in lines:
                print(line)
            print()
    if args.json:
        _json(payload)
    return 0


def run_due(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor run-due")
    parser.add_argument("--project", help="limit to one registered project")
    parser.add_argument("--schedule", help="limit to one schedule id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    if args.project and not store.get_project(args.project):
        print(f"khunkhaar monitor run-due: project '{args.project}' is not registered")
        return 2
    result = runner.run_due(project_id=args.project, schedule_id=args.schedule)
    if args.json:
        _json(result)
    else:
        _print_run(result)
    return _EXIT.get(str(result.get("status")), 0)


def _print_run(result: dict) -> None:
    print(f"now             {result.get('now')}")
    print(f"status          {result.get('status')}")
    if not result.get("enabled"):
        print(result.get("note"))
        return
    print(f"executed        {result.get('executed', 0)}")
    print(f"missed          {result.get('missed', 0)}")
    for item in result.get("schedules") or []:
        print(f"  {item.get('schedule_id')} {item.get('name')}: {item.get('status')} - "
              f"{item.get('note')}")
        if item.get("evidence"):
            print(f"    evidence    {item.get('evidence')}")
    print("a missed window means no check ran while the machine was off; it is never reported "
          "as a successful check, and environment_unavailable is never a defect")


def pause_or_resume(argv: list[str], *, resume: bool) -> int:
    name = "resume" if resume else "pause"
    parser = argparse.ArgumentParser(prog=f"khunkhaar monitor {name}")
    parser.add_argument("--schedule", required=True, help="schedule id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    try:
        schedule = schedules.set_enabled(args.schedule, resume)
    except schedules.ScheduleError as exc:
        print(f"khunkhaar monitor {name}: {exc}")
        return 1
    payload = {"schedule_id": schedule["schedule_id"], "enabled": schedule["enabled"],
               "paused_at": schedule.get("paused_at"), "next_due_at": schedule.get("next_due_at"),
               "note": ("paused; a pause never creates a missed run" if not resume
                        else "resumed; the next window was recalculated from the current time")}
    if args.json:
        _json(payload)
        return 0
    print(f"{name}d {schedule['schedule_id']}: {payload['note']}")
    if resume:
        print(f"next due        {payload['next_due_at']}")
    return 0


def run_pause(argv: list[str]) -> int:
    return pause_or_resume(argv, resume=False)


def run_resume(argv: list[str]) -> int:
    return pause_or_resume(argv, resume=True)


def list_schedules(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor list")
    parser.add_argument("--project", help="registered project id")
    parser.add_argument("--path", help="registered repository")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    project_id = None
    if args.project or args.path:
        project_id, refusal = _resolve_project("monitor list", args.project, args.path)
        if refusal:
            print(refusal)
            return 2
    rows = store.list_monitoring_schedules(project_id)
    payload = {"project_id": project_id, "count": len(rows), "schedules": rows,
               "monitoring_enabled": bool(current().get("monitoring", "enabled", False))}
    if args.json:
        _json(payload)
        return 0
    if not rows:
        print("no monitoring schedules exist. Create one with `khunkhaar monitor set`.")
        return 0
    print(f"{'SCHEDULE':<14}{'PROJECT':<14}{'NAME':<20}{'EXPRESSION':<16}{'ENABLED':<9}NEXT DUE")
    for row in rows:
        print(f"{row['schedule_id']:<14}{str(row.get('project_id')):<14}"
              f"{str(row.get('name'))[:18]:<20}{str(row.get('expression')):<16}"
              f"{'yes' if row.get('enabled') else 'no':<9}{row.get('next_due_at')}")
    if not payload["monitoring_enabled"]:
        print("monitoring is disabled in config; run-due dispatches nothing until it is enabled")
    return 0


def status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor status")
    parser.add_argument("--project", help="registered project id")
    parser.add_argument("--path", help="registered repository")
    parser.add_argument("--schedule", help="one schedule id")
    parser.add_argument("--grace", type=int, default=None,
                        help="seconds a due window stays 'due' (default from config)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    grace = args.grace if args.grace is not None else int(
        current().get("monitoring", "schedule_grace_seconds", 900))
    if args.schedule:
        schedule = store.get_monitoring_schedule(args.schedule)
        if not schedule:
            print(f"khunkhaar monitor status: schedule '{args.schedule}' does not exist")
            return 1
        rows = [schedule]
    else:
        project_id = None
        if args.project or args.path:
            project_id, refusal = _resolve_project("monitor status", args.project, args.path)
            if refusal:
                print(refusal)
                return 2
        rows = store.list_monitoring_schedules(project_id)

    now = schedules.utcnow().astimezone(timezone.utc)
    states = [schedules.state(row, now, grace_seconds=max(0, grace)) for row in rows]
    payload = {"now": schedules.stamp(now),
               "monitoring_enabled": bool(current().get("monitoring", "enabled", False)),
               "grace_seconds": grace, "count": len(states), "schedules": states,
               "wording": ("'missed' states a window passed without a recorded run-due; it never "
                           "claims a check ran")}
    if args.json:
        _json(payload)
        return 0
    if not states:
        print("no monitoring schedules exist.")
        return 0
    for item in states:
        print(f"{item['schedule_id']:<14}{item['state']:<12}{str(item.get('name')):<20}"
              f"{item.get('next_due_at')}  {item.get('note')}")
        if item.get("artifact_problem"):
            print(f"  artifact      {item['artifact_problem']}")
    if not payload["monitoring_enabled"]:
        print("monitoring is disabled in config; every state above is informational only")
    return 0


def history(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar monitor history")
    parser.add_argument("--project", help="registered project id")
    parser.add_argument("--path", help="registered repository")
    parser.add_argument("--schedule", help="one schedule id")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    project_id = None
    if args.project or args.path:
        project_id, refusal = _resolve_project("monitor history", args.project, args.path)
        if refusal:
            print(refusal)
            return 2
    rows = store.list_monitoring_runs(project_id, args.schedule, limit=max(1, args.limit))
    payload = {"project_id": project_id, "schedule_id": args.schedule, "count": len(rows),
               "runs": rows}
    if args.json:
        _json(payload)
        return 0
    if not rows:
        print("no monitoring run is recorded. A window is recorded only when run-due processes it.")
        return 0
    print(f"{'RUN':<22}{'SCHEDULE':<14}{'DUE AT':<22}{'STATUS':<22}{'STARTED':<22}FINISHED")
    for row in rows:
        print(f"{str(row.get('run_id') or '-'):<22}{row['schedule_id']:<14}"
              f"{str(row.get('due_at')):<22}{str(row.get('status')):<22}"
              f"{str(row.get('started_at') or '-'):<22}{row.get('finished_at') or '-'}")
        if row.get("note"):
            print(f"  {row['note']}")
    return 0
