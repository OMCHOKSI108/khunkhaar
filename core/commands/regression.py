"""`khunkhaar regression` - generated tests, validation and budgeted execution.

Generation only reads the findings store. Validation and execution send requests to a target, so
both refuse to start unless the target passes the same outbound policy as every other Phase 3
request and, for a non-loopback host, has an unexpired authorization grant for this project.
`--show-plan` prints the selection and stops: reviewing a plan never executes it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from core.shared.target_policy import preflight_target as _preflight_target
from core.metadata import store
from core.project_discovery import detector
from core.regression import generate, runner, select, validate
from core.shared import paths
from core.shared.config import current

_FAILING_VERDICTS = {"unreliable", "not_reproduced"}


def _resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _project_id(root: Path) -> str | None:
    project_id = detector.project_id_for(root)
    return project_id if store.get_project(project_id) else None


def _require_project(prefix: str, root: Path) -> tuple[str | None, str | None]:
    project_id = _project_id(root)
    if project_id:
        return project_id, None
    return None, (f"khunkhaar {prefix}: {root} is not a registered project. Run "
                  f"`khunkhaar project register --path {root}` first.")


def _test_rows(project_id: str) -> list[dict]:
    rows = []
    for test in store.list_regression_tests(project_id):
        latest = store.list_regression_runs(project_id, test_id=str(test["test_id"]), limit=1)
        rows.append({**test, "latest_run": latest[0] if latest else None})
    return rows


def run_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar regression list")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--finding", help="only tests generated for this finding id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("regression list", root)
    if refusal:
        print(refusal)
        return 2

    rows = _test_rows(project_id)
    if args.finding:
        rows = [row for row in rows if row.get("finding_id") == args.finding]
    payload = {
        "project_id": project_id,
        "tests": rows,
        "counts": {
            "tests": len(rows),
            "by_reliability": _count(rows, "reliability"),
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print("no regression tests are recorded for this project. Run "
              "`khunkhaar regression generate --path <repository>` to generate tests from "
              "independently confirmed findings.")
        return 0
    print(f"{'TEST':<15}{'FINDING':<15}{'RELIABILITY':<14}{'CONF':<7}{'METHOD':<10}OBJECTIVE")
    for row in rows:
        print(f"{row['test_id']:<15}{str(row.get('finding_id')):<15}"
              f"{str(row.get('reliability')):<14}{float(row.get('confidence') or 0):<7.2f}"
              f"{str(row.get('method')):<10}{str(row.get('objective'))[:70]}")
    for row in rows:
        latest = row.get("latest_run") or {}
        print(f"  {row['test_id']}: last run {latest.get('result') or 'never'}"
              f" at {latest.get('finished_at') or '-'}")
    return 0


def run_generate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar regression generate")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--finding", action="append", default=[],
                        help="generate only for this finding id (repeatable)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("regression generate", root)
    if refusal:
        print(refusal)
        return 2

    selection = select.select(project_id)
    eligible = selection["eligible"]
    if args.finding:
        wanted = set(args.finding)
        eligible = [item for item in eligible if item.get("id") in wanted]
    generated = [generate.generate(finding, project_id=project_id) for finding in eligible]
    payload = {
        "project_id": project_id,
        "generated": [
            {"test_id": item["test"]["test_id"], "finding_id": item["test"]["finding_id"],
             "reliability": item["test"]["reliability"], "changed": item["changed"],
             "path": item["path"], "intent_path": item["intent_path"]}
            for item in generated
        ],
        "eligible": len(selection["eligible"]),
        "excluded": selection["excluded"],
        "wording": ("only findings an independent verifier marked confirmed are eligible; the "
                    "excluded list records why every other finding was not generated"),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    for item in payload["generated"]:
        state = "updated" if item["changed"] else "unchanged"
        print(f"{state:<10}{item['test_id']:<15}{item['finding_id']:<15}{item['path']}")
    if not payload["generated"]:
        print("no eligible finding was selected; only independently confirmed findings generate "
              "regression tests")
    for item in payload["excluded"][:20]:
        print(f"excluded  {item.get('finding_id')} [{item.get('status')}]: {item.get('reason')}")
    if len(payload["excluded"]) > 20:
        print(f"          ... {len(payload['excluded']) - 20} more excluded finding(s); use --json "
              f"for the full list")
    return 0


def _select_for_validation(project_id: str, wanted: list[str]) -> list[dict]:
    tests = store.list_regression_tests(project_id)
    if wanted:
        wanted_ids = set(wanted)
        return [test for test in tests if test["test_id"] in wanted_ids]
    return [test for test in tests if test.get("reliability") == "generated"]


def run_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar regression validate")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--test", action="append", default=[],
                        help="test id to validate (repeatable; default: every generated test)")
    parser.add_argument("--target", required=True, help="base URL of the test environment")
    parser.add_argument("--users", help="JSON file mapping role -> {username,password}")
    parser.add_argument("--db", help="SQLite database path for persistence probes")
    parser.add_argument("--runs", type=int, help="normal runs required (default from config)")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per test process")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("regression validate", root)
    if refusal:
        print(refusal)
        return 2
    refusal = _preflight_target(project_id, args.target)
    if refusal:
        print(f"khunkhaar regression validate: {refusal}")
        return 2
    tests = _select_for_validation(project_id, args.test)
    if not tests:
        print("khunkhaar regression validate: no generated test matches this selection. Run "
              "`khunkhaar regression generate` first.")
        return 0

    reports = validate.validate(
        project_id, tests, target=args.target, users_path=args.users, db_path=args.db,
        timeout=max(1.0, args.timeout), runs=args.runs)
    summary: dict[str, int] = {}
    for report in reports:
        summary[report["verdict"]] = summary.get(report["verdict"], 0) + 1
    payload = {"project_id": project_id, "target_host": urlsplit(args.target).hostname or "",
               "validation_runs": _runs_hint(args.runs), "reports": reports,
               "summary": summary,
               "wording": ("environment_unavailable is an infrastructure result, not evidence "
                           "of a defect")}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return _validate_code(summary)
    for report in reports:
        print(f"{report['verdict']:<22}{report['test_id']:<15}{report.get('note')}")
    print(f"summary {summary}")
    print("promotion requires a violation in every normal run and a passing inverted run; "
          "environment_unavailable is not an application result")
    return _validate_code(summary)


def _validate_code(summary: dict[str, int]) -> int:
    if any(name in summary for name in _FAILING_VERDICTS):
        return 1
    if summary.get("environment_unavailable"):
        return 3
    return 0


def _runs_hint(override: int | None) -> int:
    value = override if override is not None else int(
        current().get("regression", "validation_runs", 2))
    return max(1, min(value, 3))


def _latest_changeset(project_id: str) -> tuple[dict | None, str | None]:
    row = store.latest_git_analysis(project_id)
    if not row:
        return None, ("no saved git analysis exists for this project. Run "
                      "`khunkhaar git analyze --path <repository>` first; impact selection "
                      "cannot invent a change set.")
    artifact = row.get("artifact")
    if not artifact:
        return None, "the latest git analysis has no artifact; re-run `khunkhaar git analyze`."
    target = Path(str(artifact))
    if not target.is_absolute():
        target = paths.kh_root() / target
    try:
        resolved = target.resolve()
        resolved.relative_to(paths.kh_root().resolve())
    except (OSError, ValueError):
        return None, "the latest git analysis artifact is outside the KHUNKHAAR root; refusing to read it."
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, (f"the latest git analysis artifact could not be read "
                      f"({type(exc).__name__}: {exc}); re-run `khunkhaar git analyze`.")
    changeset = document.get("changeset") if isinstance(document, dict) else None
    if not isinstance(changeset, dict):
        return None, ("the latest git analysis artifact has no change set; re-run "
                      "`khunkhaar git analyze`.")
    return changeset, None


def _changed_paths(changeset: dict) -> list[str]:
    paths = {str(item.get("path")) for item in changeset.get("files") or []
             if isinstance(item, dict) and item.get("path")}
    paths |= {str(item.get("old_path")) for item in changeset.get("files") or []
              if isinstance(item, dict) and item.get("old_path")}
    return sorted(path for path in paths if path)


def run_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar regression run")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--selector", choices=("all", "impact"), default="all",
                        help="which tests to run (default: all non-quarantined tests)")
    parser.add_argument("--target", required=True, help="base URL of the test environment")
    parser.add_argument("--users", help="JSON file mapping role -> {username,password}")
    parser.add_argument("--db", help="SQLite database path for persistence probes")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per test process")
    parser.add_argument("--max-seconds", type=float, default=900.0, help="wall-clock budget")
    parser.add_argument("--show-plan", action="store_true",
                        help="print the selection and stop without executing anything")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("regression run", root)
    if refusal:
        print(refusal)
        return 2

    changeset = None
    if args.selector == "impact":
        changeset, refusal = _latest_changeset(project_id)
        if refusal:
            print(f"khunkhaar regression run: {refusal}")
            return 2
        selection = runner.select_by_impact(project_id, _changed_paths(changeset))
    else:
        selection = runner.select_all(project_id)

    plan = {
        "project_id": project_id,
        "selector": args.selector,
        "target_host": urlsplit(args.target).hostname or "",
        "selection": selection["selected"],
        "excluded": selection["excluded"],
        "budget": {"max_seconds": args.max_seconds,
                   "max_tests": len(selection["selected"]),
                   "test_timeout_seconds": args.timeout},
        "changeset": None if changeset is None else {
            "kind": changeset.get("kind"), "base": changeset.get("base"),
            "head": changeset.get("head"), "changed_paths": _changed_paths(changeset),
        },
        "wording": ("selection is a plan, not an execution: a reviewed plan is executed only "
                    "when --show-plan is absent"),
    }
    if args.show_plan:
        payload = {"plan": plan, "executed": False,
                   "note": "--show-plan requested; nothing was executed"}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return 0
        _print_plan(plan)
        print("nothing was executed (--show-plan)")
        return 0

    refusal = _preflight_target(project_id, args.target)
    if refusal:
        print(f"khunkhaar regression run: {refusal}")
        return 2
    if not selection["selected"]:
        print("khunkhaar regression run: no test is selected. Nothing was executed.")
        for item in selection["excluded"][:10]:
            print(f"excluded  {item.get('test_id')}: {item.get('reason')}")
        return 0

    execution = runner.execute(
        project_id, selection, target=args.target, users_path=args.users, db_path=args.db,
        timeout=max(1.0, args.timeout), max_seconds=max(1.0, args.max_seconds))
    payload = {"plan": plan, "executed": True, "execution": execution}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return _run_code(execution["summary"])
    _print_plan(plan)
    for item in execution["results"]:
        print(f"{item['result']:<22}{item['test_id']:<15}{item.get('note')}")
    print(f"summary {execution['summary']['by_result']} in {execution['run_id']}")
    print("pass = the recorded defect was not observed; fail = it still reproduces; "
          "environment_unavailable = no conclusion about the application")
    return _run_code(execution["summary"])


def _run_code(summary: dict) -> int:
    counts = summary.get("by_result") or {}
    if counts.get("fail"):
        return 1
    if counts.get("environment_unavailable"):
        return 2
    if counts.get("not_run"):
        return 3
    return 0


def _print_plan(plan: dict) -> None:
    print(f"selector        {plan['selector']}")
    print(f"target host     {plan['target_host']}")
    print(f"budget          {plan['budget']['max_tests']} test(s), "
          f"{plan['budget']['max_seconds']:.0f}s wall clock, "
          f"{plan['budget']['test_timeout_seconds']:.0f}s per test")
    if plan.get("changeset"):
        changeset = plan["changeset"]
        print(f"change set      {changeset.get('kind')} {changeset.get('base') or '-'} -> "
              f"{changeset.get('head') or '-'} ({len(changeset.get('changed_paths') or [])} path(s))")
    if not plan["selection"]:
        print("selected        none")
    for item in plan["selection"]:
        test = item["test"]
        print(f"selected        {test['test_id']:<15}{test.get('reliability'):<12}{item['reason']}")
    for item in plan["excluded"][:10]:
        print(f"excluded        {item.get('test_id')}: {item.get('reason')}")
    print(f"wording         {plan['wording']}")


def _count(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts
