"""`khunkhaar impact` - Module C: what a saved change set can reach, and an approximate risk.

The input is the artifact of the most recent `khunkhaar git analyze` for the registered project,
because the metadata row keeps counts only. The graph is built from the current checkout when none
exists (or when `--rebuild-graph` is given). Nothing in the repository is written or executed, and
the report is saved as a 0600 artifact plus a metadata row that stores the factor contributions and
no repository content.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.findings import store as findings_store
from core.impact import graph as impact_graph
from core.impact import risk as impact_risk
from core.impact import traverse
from core.metadata import store
from core.project_discovery import detector
from core.shared import evidence, paths
from core.shared.config import current


def _resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="khunkhaar impact")
    parser.add_argument("--path", default=".", help="registered repository the analysis belongs to")
    parser.add_argument("--depth", type=int, default=None,
                        help="reverse dependency hops (default: impact.max_depth in config)")
    parser.add_argument("--limit", type=int, default=40, help="rows printed per section")
    parser.add_argument("--rebuild-graph", action="store_true",
                        help="rebuild the dependency graph from the current checkout first")
    parser.add_argument("--save", dest="save", action="store_true", default=True,
                        help="write the impact report artifact and row (default)")
    parser.add_argument("--no-save", dest="save", action="store_false",
                        help="print only; nothing is written anywhere")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str]) -> int:
    args = _args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id = detector.project_id_for(root)
    if not project_id or not store.get_project(project_id):
        print(f"khunkhaar impact: {root} is not a registered project. Run "
              f"`khunkhaar project register --path {root}` first - impact analysis never runs "
              f"against a repository that was not added on purpose.")
        return 2

    analysis_row, changeset, refusal = _load_analysis(project_id)
    if refusal:
        print(f"khunkhaar impact: {refusal}")
        return 2

    app_map = None
    rebuilt = False
    graph = None if args.rebuild_graph else impact_graph.load(project_id)
    if graph is None:
        try:
            app_map = _app_map(root, project_id)
            built = impact_graph.build(project_id, root, app_map=app_map, persist=args.save)
        except Exception as exc:  # a build failure is reported, never replaced by an empty graph
            print(f"khunkhaar impact: the dependency graph could not be built: "
                  f"{type(exc).__name__}: {exc}")
            return 1
        graph = built["graph"]
        rebuilt = True
        graph_artifact = built["artifact"]
    else:
        graph_artifact = str(impact_graph.graph_path(project_id))

    if app_map is None:
        app_map = _app_map(root, project_id)
    findings = findings_store.list_findings(project_id)
    last_green = store.last_green_regression_run(project_id)

    depth = args.depth if args.depth is not None else int(
        current().get("impact", "max_depth", traverse.DEFAULT_DEPTH))
    depth = max(1, min(depth, 10))
    max_nodes = int(current().get("impact", "max_impacted", traverse.DEFAULT_MAX_NODES))
    result = traverse.analyze(
        graph, project_id=project_id, changeset=changeset,
        analysis={"analysis_id": analysis_row.get("id"), "kind": analysis_row.get("kind"),
                  "base": analysis_row.get("base_ref"), "head": analysis_row.get("head_ref"),
                  "created_at": analysis_row.get("created_at"),
                  "artifact": analysis_row.get("artifact")},
        depth=depth, max_nodes=max_nodes)
    risk = impact_risk.score(graph=graph, impact=result, changeset=changeset, app_map=app_map,
                             findings=findings, last_green=last_green)

    payload = {
        "project_id": project_id,
        "repository": str(root),
        "analysis": result.analysis,
        "changeset": {key: changeset.get(key) for key in ("kind", "base", "head", "summary")},
        "graph": {"stats": graph.stats(), "artifact": _relative(graph_artifact),
                  "rebuilt": rebuilt},
        "impact": result.to_dict(),
        "risk": risk,
        "wording": impact_risk.WORDING,
        "saved": False,
        "artifact": None,
        "report_id": None,
    }
    if args.save:
        _persist(project_id, payload, risk, analysis_row)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    _print(payload, result, risk, args.limit)
    return 0


def _load_analysis(project_id: str) -> tuple[dict, dict, str | None]:
    row = store.latest_git_analysis(project_id)
    if not row:
        return {}, {}, ("no saved git analysis exists for this project. Run "
                        "`khunkhaar git analyze --path <repository>` first; impact analysis "
                        "describes a change set, it cannot invent one.")
    artifact = row.get("artifact")
    if not artifact:
        return row, {}, ("the latest git analysis has no artifact path, so its change set is not "
                         "available. Re-run `khunkhaar git analyze`.")
    target = Path(artifact)
    if not target.is_absolute():
        target = paths.kh_root() / target
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return row, {}, (f"the latest git analysis artifact could not be read "
                         f"({type(exc).__name__}: {exc}). Re-run `khunkhaar git analyze`.")
    changeset = payload.get("changeset") if isinstance(payload, dict) else None
    if not isinstance(changeset, dict):
        return row, {}, ("the latest git analysis artifact does not contain a change set. Re-run "
                         "`khunkhaar git analyze`.")
    return row, changeset, None


def _app_map(root: Path, project_id: str) -> dict:
    from core.impact import resolve

    try:
        return resolve.app_map_for(root, project_id)
    except Exception as exc:
        print(f"khunkhaar impact: the application map could not be loaded "
              f"({type(exc).__name__}: {exc}); cross-tool links are skipped")
        return {}


def _persist(project_id: str, payload: dict, risk: dict, analysis_row: dict) -> None:
    run_id = evidence.new_run_id("impact")
    directory = paths.evidence_dir(project_id, "impact-report")
    head = evidence.slugify(str(payload["changeset"].get("head") or "head"))
    name = f"{run_id}-{payload['changeset'].get('kind') or 'changes'}-{head}.json"
    artifact_payload = {key: value for key, value in payload.items()
                        if key not in ("artifact", "report_id")}
    artifact_payload["saved"] = True
    target = evidence.write_json(directory, name, artifact_payload, project_id=project_id,
                                 run_id=run_id, kind="impact-report")
    payload["saved"] = True
    payload["artifact"] = str(target)
    try:
        report_id = store.record_impact_report(
            project_id, analysis_id=analysis_row.get("id"),
            base_ref=payload["changeset"].get("base"),
            head_ref=payload["changeset"].get("head"), risk_score=risk["score"],
            risk_factors=risk["factors"], artifact=evidence.relative(target))
        payload["report_id"] = report_id
    except Exception as exc:  # the artifact is the deliverable; say the row failed, do not hide it
        print(f"khunkhaar impact: the report artifact was written but the metadata row failed: "
              f"{type(exc).__name__}: {exc}")


def _relative(value: str | None) -> str | None:
    if not value:
        return None
    target = Path(value)
    if target.is_absolute():
        return evidence.relative(target)
    return value


def _print(payload: dict, result: traverse.ImpactResult, risk: dict, limit: int) -> None:
    analysis = payload["analysis"]
    print(f"analysis        #{analysis.get('analysis_id')} {analysis.get('kind') or '-'} "
          f"{analysis.get('base') or '-'} -> {analysis.get('head') or '-'}"
          f"  ({analysis.get('created_at') or 'time unrecorded'})")
    summary = payload["changeset"].get("summary") or {}
    print(f"changes         {summary.get('files_changed', 0)} file(s), "
          f"+{summary.get('insertions', 0)} -{summary.get('deletions', 0)}, "
          f"{summary.get('renames', 0)} rename(s), {summary.get('removed', 0)} removed")
    stats = payload["graph"]["stats"]
    scanned = f", {stats['files_scanned']} file(s) scanned" if stats.get("files_scanned") else ""
    print(f"graph           {stats['nodes']} node(s), {stats['edges']} edge(s), "
          f"{stats.get('inferred_nodes', 0)} inferred node(s){scanned}"
          + ("  [rebuilt]" if payload["graph"]["rebuilt"] else ""))
    print(f"depth           {result.depth} hop(s), {payload['impact']['summary']['impacted']} "
          f"impacted node(s)")

    print("seeds")
    for item in result.seeds[:limit]:
        marker = "  [inferred]" if item["inferred"] else ""
        print(f"  {item['type']:<8}{item['name']:<34}{item['reason']}{marker}")
    if not result.seeds:
        print("  (none)")

    print("impacted")
    for item in result.impacted[:limit]:
        marker = f"  [inferred {item['confidence']:.2f}]" if item["inferred"] else ""
        location = f"  ({item['path']})" if item.get("path") else ""
        print(f"  d{item['depth']} {item['type']:<8}{item['name']:<34}{item['via']}{location}"
              f"{marker}")
    if len(result.impacted) > limit:
        print(f"  ... {len(result.impacted) - limit} more (raise --limit or read the artifact)")
    if not result.impacted:
        print("  (none)")

    print("surface")
    for item in result.surface[:limit]:
        print(f"  {item['type']:<8}{item['name']:<34}{item['via']}")
    if not result.surface:
        print("  (none)")

    if result.workflows:
        print("workflows       " + ", ".join(item["name"] for item in result.workflows[:limit]))
    if result.journeys:
        print("journeys        " + ", ".join(item["name"] for item in result.journeys[:limit]))
    if result.tests:
        print("tests           " + ", ".join(item["path"] or item["name"]
                                             for item in result.tests[:limit]))
    else:
        print("tests           none linked")

    if result.missing:
        print("not represented")
        for item in result.missing[:limit]:
            print(f"  {item['status']:<9}{item['path']}  -  {item['reason']}")

    print(f"risk            {risk['score']:.2f} / 100  [{risk['attention_band']}]")
    for factor in risk["factors"]:
        print(f"  {factor['name']:<24}weight {factor['weight']:.2f}  "
              f"normalised {factor['normalized']:.2f}  contribution {factor['contribution']:.2f}")
    print(f"  {risk['wording']}")

    for note in result.notes:
        print(f"note            {note}")
    for note in risk["notes"]:
        print(f"note            {note}")
    if payload.get("report_id"):
        print(f"report          {payload['report_id']}")
    if payload.get("artifact"):
        print(f"artifact        {payload['artifact']}")
