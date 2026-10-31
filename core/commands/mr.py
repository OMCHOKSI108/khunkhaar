"""`khunkhaar mr analyze` - Module B: a merge-request review from local refs.

The comparison is always a merge-base (three-dot) read of the local repository, so nothing is
fetched and the target branch moving after the source branched does not leak into the diff. The
review is composed from Module A's change set, Module C's reverse dependency walk and risk
factors, and the Phase 2 findings store. The optional GitLab call only enriches the header with
read-only metadata; when it is disabled, unreachable or unauthorised the local review is unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.findings import store as findings_store
from core.git import analysis as git_analysis
from core.git.runner import (GitError, RepositoryUntrustedError, StateChangedError, open_repository,
                             validate_ref)
from core.impact import graph as impact_graph
from core.impact import risk as impact_risk
from core.impact import traverse
from core.integrations import gitlab as gitlab_adapter
from core.metadata import store
from core.mr import compose
from core.project_discovery import detector
from core.shared import evidence, paths
from core.shared.config import current


def _resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="khunkhaar mr analyze")
    parser.add_argument("--path", default=".", help="registered repository to review")
    parser.add_argument("--base", "--mr-target", dest="base", metavar="REF",
                        help="merge-request target branch (default: configured base branch)")
    parser.add_argument("--head", "--mr-source", dest="head", metavar="REF",
                        help="merge-request source branch (default: HEAD)")
    parser.add_argument("--gitlab-mr", metavar="IID",
                        help="also fetch read-only metadata for this merge request from GitLab; "
                             "disabled by default and never required for the local review")
    parser.add_argument("--gitlab-project", metavar="PATH",
                        help="GitLab project path or id for this run (default: gitlab.project)")
    parser.add_argument("--depth", type=int, default=None,
                        help="reverse dependency hops (default: impact.max_depth in config)")
    parser.add_argument("--limit", type=int, default=20, help="rows printed per section")
    parser.add_argument("--rebuild-graph", action="store_true",
                        help="rebuild the dependency graph from the current checkout first")
    parser.add_argument("--save", dest="save", action="store_true", default=True,
                        help="write the review artifact and rows (default)")
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
        print(f"khunkhaar mr analyze: {root} is not a registered project. Run "
              f"`khunkhaar project register --path {root}` first - a merge-request review never "
              f"runs against a repository that was not added on purpose.")
        return 2

    gitlab: dict = {"status": "disabled", "reason": "no --gitlab-mr was requested",
                    "merge_request": None}
    if args.gitlab_mr:
        gitlab = gitlab_adapter.fetch_merge_request(args.gitlab_mr, project=args.gitlab_project)
    metadata = gitlab.get("merge_request") or {}
    head = args.head or str(metadata.get("source_branch") or "")
    base = args.base or str(metadata.get("target_branch") or "")

    try:
        repo = open_repository(root)
        changeset = git_analysis.analyze(repo, base=base or None, head=head or None, mr=True)
        head_ref = validate_ref(head) if head else "HEAD"
        target_ref = changeset.base
        local_refs = {
            "source_ref": head_ref,
            "target_ref": target_ref,
            "head_sha": repo.verify_ref(head_ref) or "",
            "base_sha": repo.verify_ref(target_ref) if target_ref else "",
            "server_sha_present": bool(metadata.get("sha")
                                       and repo.verify_ref(str(metadata["sha"]))),
        }
        if gitlab.get("status") == gitlab_adapter.STATUS_OK:
            gitlab = gitlab_adapter.compare_local(gitlab, local_refs)
    except (RepositoryUntrustedError, GitError) as exc:
        code = 2 if isinstance(exc, RepositoryUntrustedError) else 1
        print(f"khunkhaar mr analyze: {exc}")
        return code
    except StateChangedError as exc:
        print(f"khunkhaar mr analyze: {exc}")
        return 3

    rebuilt = False
    graph = None if args.rebuild_graph else impact_graph.load(project_id)
    if graph is None:
        try:
            app_map = _app_map(root, project_id)
            built = impact_graph.build(project_id, root, app_map=app_map, persist=args.save)
        except Exception as exc:  # a build failure is reported, never replaced by an empty graph
            print(f"khunkhaar mr analyze: the dependency graph could not be built: "
                  f"{type(exc).__name__}: {exc}")
            return 1
        graph = built["graph"]
        rebuilt = True
    else:
        app_map = _app_map(root, project_id)

    findings = findings_store.list_findings(project_id)
    last_green = store.last_green_regression_run(project_id)
    depth = args.depth if args.depth is not None else int(
        current().get("impact", "max_depth", traverse.DEFAULT_DEPTH))
    depth = max(1, min(depth, 10))
    max_nodes = int(current().get("impact", "max_impacted", traverse.DEFAULT_MAX_NODES))
    changeset_dict = changeset.to_dict()
    result = traverse.analyze(
        graph, project_id=project_id, changeset=changeset_dict,
        analysis={"kind": "merge-request", "base": changeset.base, "head": changeset.head,
                  "base_source": changeset.base_source},
        depth=depth, max_nodes=max_nodes)
    risk = impact_risk.score(graph=graph, impact=result, changeset=changeset_dict,
                             app_map=app_map, findings=findings, last_green=last_green)
    payload = compose.build(
        project_id=project_id, repository=str(root), changeset=changeset_dict, graph=graph,
        impact=result, risk=risk, app_map=app_map, findings=findings, gitlab=gitlab)
    payload["graph"]["rebuilt"] = rebuilt

    if args.save:
        _persist(project_id, payload, changeset, risk)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    _print(payload, args.limit)
    return 0


def _app_map(root: Path, project_id: str) -> dict:
    from core.impact import resolve

    try:
        return resolve.app_map_for(root, project_id)
    except Exception as exc:
        print(f"khunkhaar mr analyze: the application map could not be loaded "
              f"({type(exc).__name__}: {exc}); cross-tool links are skipped")
        return {}


def _persist(project_id: str, payload: dict, changeset, risk: dict) -> None:
    run_id = evidence.new_run_id("mr")
    directory = paths.evidence_dir(project_id, "mr-analysis")
    head = evidence.slugify(str(changeset.head or "head"))
    target = evidence.write_json(
        directory, f"{run_id}-merge-request-{head}.json",
        {key: value for key, value in payload.items() if key not in ("artifact", "report_id")}
        | {"saved": True},
        project_id=project_id, run_id=run_id, kind="mr-analysis")
    payload["saved"] = True
    payload["artifact"] = str(target)
    try:
        analysis_id = store.record_git_analysis(
            project_id, run_id, "merge-request", base_ref=changeset.base,
            head_ref=changeset.head, summary=changeset.summary(),
            artifact=evidence.relative(target))
        payload["analysis_id"] = analysis_id
        payload["report_id"] = store.record_impact_report(
            project_id, analysis_id=analysis_id, base_ref=changeset.base,
            head_ref=changeset.head, risk_score=risk["score"],
            risk_factors=risk["factors"], artifact=evidence.relative(target))
    except Exception as exc:  # the artifact is the deliverable; say the row failed, do not hide it
        print(f"khunkhaar mr analyze: the artifact was written but a metadata row failed: "
              f"{type(exc).__name__}: {exc}")


def _print(payload: dict, limit: int) -> None:
    mr = payload["merge_request"]
    summary = mr["summary"]
    print(f"repository      {payload['repository']}")
    print(f"comparison      merge-request: {mr['target'] or '-'} -> {mr['source'] or '-'}"
          f"  [merge base, base source {mr['base_source']}]")
    print(f"changes         {summary.get('files_changed', 0)} file(s), "
          f"+{summary.get('insertions', 0)} -{summary.get('deletions', 0)}, "
          f"{summary.get('commits', 0)} commit(s), {summary.get('renames', 0)} rename(s)")

    print("modules")
    for item in payload["modules"][:limit]:
        languages = f"  [{', '.join(item['languages'])}]" if item["languages"] else ""
        print(f"  {item['module']:<20}{item['files']} file(s)  "
              f"+{item['insertions']} -{item['deletions']}  {item['symbols']} symbol(s)"
              f"{languages}")
    if not payload["modules"]:
        print("  (none)")

    affected = payload["affected"]
    _list_line("services", [item["name"] for item in affected["services"]], limit)
    _list_line("api endpoints", [f"{item['name']}  ({item['via']})"
                                 for item in affected["api_endpoints"]], limit)
    _list_line("graphql", [f"{item['name']}  ({item['via']})"
                           for item in affected["graphql_operations"]], limit)
    _list_line("db models", [f"{item['name']}  ({item['via']})"
                             for item in affected["database_models"]], limit)
    _list_line("workflows", [item["name"] for item in affected["workflows"]], limit)
    _list_line("journeys", [item["name"] for item in affected["journeys"]], limit)
    _list_line("linked tests", [str(item.get("path") or item.get("name"))
                                for item in affected["tests"]], limit)

    print("security        " + payload["security"]["implications"][0])
    for implication in payload["security"]["implications"][1:]:
        print(f"                {implication}")

    print("regressions")
    if payload["regressions"]["historical_findings"]:
        for item in payload["regressions"]["historical_findings"][:limit]:
            print(f"  {item['id']:<10}{item['status']:<14}{item['title']}"
                  f"  ({', '.join(item['paths'][:3])})")
    else:
        print("  no earlier finding references a changed or reached path")
    coverage = payload["regressions"]["coverage"]
    print(f"  coverage      {coverage.get('source_files_changed', 0)} source file(s), "
          f"{coverage.get('without_linked_test', 0)} without a linked test")
    for note in payload["regressions"]["notes"]:
        print(f"  note          {note}")

    risk = payload["risk"]
    print(f"risk            {risk['score']:.2f} / 100  [{risk['attention_band']}]")
    for factor in risk["factors"]:
        print(f"  {factor['name']:<24}weight {factor['weight']:.2f}  "
              f"normalised {factor['normalized']:.2f}  contribution {factor['contribution']:.2f}")
    print(f"  {risk['wording']}")

    print(f"recommended QA plan  ({payload['plan_execution']})")
    for item in payload["recommended_qa_plan"]:
        print(f"  [{item['priority']:<6}] {item['area']:<11}{item['action']}")
        print(f"  {'':<9} {item['why']}")

    gitlab = payload["gitlab"]
    detail = f"gitlab          {gitlab.get('status')}: {gitlab.get('reason')}"
    merge_request = gitlab.get("merge_request")
    if merge_request:
        detail += (f"  !{merge_request.get('iid')} {merge_request.get('title')}"
                   f"  [{merge_request.get('state')}]")
    print(detail)
    local = gitlab.get("local") or {}
    for note in local.get("notes") or []:
        print(f"                {note}")

    for note in payload["notes"]:
        print(f"note            {note}")
    if payload.get("analysis_id"):
        print(f"analysis        #{payload['analysis_id']}")
    if payload.get("report_id"):
        print(f"report          {payload['report_id']}")
    if payload.get("artifact"):
        print(f"artifact        {payload['artifact']}")


def _list_line(label: str, values: list[str], limit: int) -> None:
    print(f"{label:<16}" + (", ".join(values[:limit]) if values else "(none)"))
    if len(values) > limit:
        print(f"  ... {len(values) - limit} more")
