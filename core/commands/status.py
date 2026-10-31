"""`khunkhaar status` — one screen that says whether the platform is usable right now."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from core.metadata import migrations, store
from core.project_discovery import detector
from core.shared import paths


def _payload(project_root: str = ".") -> dict[str, Any]:
    stats = store.stats()
    schema = migrations.status()
    profile = detector.detect(Path(project_root).expanduser().resolve())
    projects = store.list_projects()
    health = store.tool_health()
    free = shutil.disk_usage(paths.kh_root()).free
    blocked = [entry for entry in health if entry["status"] not in ("ok", "needs-env",
                                                                   "not-running")]
    return {
        "version": paths.version(),
        "root": str(paths.kh_root()),
        "schema": schema,
        "cwd_project": {
            "project_id": profile.get("project_id"),
            "detected": bool(profile.get("valid_project")),
            "reason": profile.get("reason"),
            "registered": bool(profile.get("valid_project") and any(
                p["project_id"] == profile["project_id"] for p in projects)),
        },
        "projects": len(projects),
        "counts": {k: v for k, v in stats.items() if k != "schema_version"},
        "tool_health": {"checked": len(health), "needs_attention": [
            {"tool": e["tool"], "status": e["status"], "detail": (e.get("detail") or "")[:80]}
            for e in blocked]},
        "authorizations": len(store.active_authorizations()),
        "disk_free_gib": round(free / 1024**3, 1),
    }


def paths_absolute(project_root: str):
    from pathlib import Path

    return Path(project_root).expanduser().resolve()


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar status")
    parser.add_argument("--project-root", default=".", help="repository to report on")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store.init()
    data = _payload(args.project_root)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    schema = data["schema"]
    print(f"KHUNKHAAR QA {data['version']}  (root: {data['root']})")
    print(f"  schema        v{schema['current']} of v{schema['latest']}"
          f"{' - ' + str(schema['pending']) + ' pending' if schema['pending'] else ''}")
    cwd = data["cwd_project"]
    if cwd["detected"]:
        print(f"  here          {cwd['project_id']} "
              f"({'registered' if cwd['registered'] else 'not registered'})")
    else:
        print(f"  here          {cwd['reason']}")
    counts = data["counts"]
    print("  records       " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"  projects      {data['projects']}   active authorizations: {data['authorizations']}")
    health = data["tool_health"]
    print(f"  tool health   {health['checked']} checked, "
          f"{len(health['needs_attention'])} need attention")
    for entry in health["needs_attention"][:8]:
        print(f"    {entry['tool']:<26}{entry['status']:<14}{entry['detail']}")
    print(f"  disk          {data['disk_free_gib']} GiB free")
    return 0
