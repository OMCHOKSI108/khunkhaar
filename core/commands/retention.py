"""`khunkhaar retention index|plan|apply` — disk usage you can see before you trust it.

`apply` deletes nothing without `--confirm`, and even with it only touches `evidence/` and
`runtime/tmp/`. Files referenced by a finding that is not closed are excluded by the policy module
itself, so the guard is not just in this CLI layer.
"""

from __future__ import annotations

import argparse
import json

from core import retention as policy
from core.metadata import store
from core.shared import paths


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--days", type=int, help="override the configured evidence window")
    parser.add_argument("--class-days", action="append", metavar="CLASS=N",
                        help="per-class window, repeatable (e.g. screenshot=14)")
    parser.add_argument("--json", action="store_true")
    return parser


def _windows(args) -> tuple[int | None, dict[str, int]]:
    default, by_class = policy.windows_from_config()
    if args.days is not None:
        default = args.days or None
    for entry in args.class_days or []:
        name, _, value = entry.partition("=")
        if not value.isdigit():
            raise SystemExit(f"khunkhaar retention: --class-days expects CLASS=N, got {entry!r}")
        by_class[name.strip()] = int(value)
    return default, by_class


def index(argv: list[str]) -> int:
    parser = _parser("khunkhaar retention index")
    parser.add_argument("--rebuild", action="store_true",
                        help="clear evidence_refs first (it is derived from disk, so this is safe)")
    args = parser.parse_args(argv)
    store.init()
    if args.rebuild:
        with store.connect() as conn:
            cleared = conn.execute("DELETE FROM evidence_refs").rowcount
        print(f"cleared {cleared} index row(s)")
    runs_linked = policy.backfill_run_projects()
    result = policy.index_all()
    if args.json:
        print(json.dumps({"runs_linked": runs_linked, **result}, indent=2, sort_keys=True))
    else:
        print(f"indexed {result['added']} new artifact(s); "
              f"{result['already_indexed']} already in evidence_refs; "
              f"{result['attributed_to_project']} attributed to a project")
        print(f"qa_runs rows linked to their project: {runs_linked}")
        print(f"index: {paths.metadata_db()}")
    return 0


def plan(argv: list[str]) -> int:
    args = _parser("khunkhaar retention plan").parse_args(argv)
    store.init()
    days, by_class = _windows(args)
    result = policy.plan(days=days, by_class=by_class)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    print(f"windows: default={days if days else 'disabled'} days  "
          f"per-class={by_class or 'none'}")
    print(f"protected by open findings: {result['protected']} path(s)")
    candidates = result["candidates"]
    if not candidates:
        print("nothing eligible - nothing would be deleted")
        return 0
    print(f"{'BYTES':>10}  CLASS          PATH")
    for entry in sorted(candidates, key=lambda e: -e["bytes"])[:40]:
        print(f"{entry['bytes']:>10}  {entry['class']:<14} {entry['path']}")
    print(f"\n{len(candidates)} file(s), {result['bytes_reclaimable']} bytes reclaimable")
    print("run `khunkhaar retention apply --confirm` to delete these")
    return 0


def apply(argv: list[str]) -> int:
    parser = _parser("khunkhaar retention apply")
    parser.add_argument("--confirm", action="store_true",
                        help="actually delete; without this flag the command is a dry run")
    args = parser.parse_args(argv)
    store.init()
    days, by_class = _windows(args)
    result = policy.apply(confirm=args.confirm, days=days, by_class=by_class)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    if result["dry_run"]:
        print(f"dry run: {len(result['would_delete'])} file(s) would be deleted "
              f"({result['bytes_reclaimable']} bytes). Add --confirm to proceed.")
        return 0
    print(f"deleted {len(result['deleted'])} file(s), "
          f"{result['bytes_freed']} bytes reclaimed")
    return 0
