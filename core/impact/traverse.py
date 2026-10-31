"""Turn a change set into the set of nodes it can affect.

The graph points from dependent to dependency, so impact travels backwards: a changed file is a
seed, and everything that imports, calls, exposes or triggers it is reached by walking incoming
edges. The result keeps the path that produced each hit (`via`), its depth and the confidence of
the weakest edge used, because "this endpoint is affected because the controller imports the
service" is reviewable and "impact count: 7" is not.

Two honesty rules govern the output:

* a changed path that is absent from the graph (deleted, unsupported language, skipped directory)
  is reported as *not represented*, never silently dropped and never counted as "no impact";
* anything reached through a link that a parser did not state is marked `inferred` with the
  confidence recorded on the edge, so a naming-convention guess never reads as a fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.impact import model

DEFAULT_DEPTH = 3
DEFAULT_MAX_NODES = 500

# Types that are worth calling out as the visible surface of a change. A workflow is listed
# separately from the API it triggers because "the checkout flow is affected" is the sentence an
# operator acts on.
SURFACE_TYPES = ("api", "entity", "workflow", "journey")

_SOURCE_TYPES = ("file", "class", "symbol", "service", "config", "test")


@dataclass
class ImpactResult:
    project_id: str
    graph_root: str
    graph_generated_at: str
    analysis: dict[str, Any]
    depth: int
    seeds: list[dict[str, Any]] = field(default_factory=list)
    impacted: list[dict[str, Any]] = field(default_factory=list)
    surface: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    journeys: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "graph": {"root": self.graph_root, "generated_at": self.graph_generated_at},
            "analysis": self.analysis,
            "depth": self.depth,
            "summary": self.summary,
            "seeds": self.seeds,
            "impacted": self.impacted,
            "surface": self.surface,
            "workflows": self.workflows,
            "journeys": self.journeys,
            "tests": self.tests,
            "missing": self.missing,
            "notes": self.notes,
            "truncated": self.truncated,
        }


def analyze(graph: model.Graph, *, project_id: str, changeset: dict[str, Any],
            analysis: dict[str, Any] | None = None, depth: int | None = None,
            max_nodes: int = DEFAULT_MAX_NODES) -> ImpactResult:
    changes = list(changeset.get("files") or [])
    limit = max(1, int(depth if depth is not None else DEFAULT_DEPTH))
    analysis_info = dict(analysis or {})
    analysis_info.setdefault("kind", changeset.get("kind"))
    analysis_info.setdefault("base", changeset.get("base"))
    analysis_info.setdefault("head", changeset.get("head"))

    result = ImpactResult(project_id=project_id, graph_root=graph.root,
                          graph_generated_at=graph.generated_at, analysis=analysis_info,
                          depth=limit)
    result.notes.append(
        "the dependency graph describes the checkout that was scanned"
        + (f" at {graph.generated_at}" if graph.generated_at else "")
        + ", not the analysed revision; a file outside that checkout cannot be located and is "
          "reported under 'not represented'")

    paths = graph.path_index()
    declarations = _declaration_index(graph)
    seed_ids: dict[str, dict[str, Any]] = {}

    for change in changes:
        path = str(change.get("path") or "")
        old_path = change.get("old_path")
        status = str(change.get("status") or "modified")
        node_id = paths.get(path)
        if node_id is None and old_path:
            # A rename keeps its outgoing relationships in the new checkout; the old path only
            # helps when the new path was not scanned (for example, moved out of the project).
            node_id = paths.get(str(old_path))
        if node_id is None:
            node = None
            suffix = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
            reason = ("deleted from the analysed revision, so it has no node in the current graph"
                      if status == "deleted" else
                      "not in the scanned graph (unsupported language, skipped directory, or "
                      "created after the graph was built)")
            result.missing.append({"path": path, "old_path": old_path, "status": status,
                                   "suffix": suffix, "reason": reason, "confidence": 0.0})
            continue
        node = graph.node(node_id)
        if node is None:
            continue
        _add_seed(seed_ids, node, reason=f"{status} file")
        if node.meta.get("parsed") is False:
            result.notes.append(
                f"{path}: {node.meta.get('reason') or 'not parsed'} - impact for this file is "
                f"limited to file-level links and is labelled inferred")

        for symbol in change.get("symbols") or []:
            name = str(symbol.get("name") or "")
            if not name:
                continue
            declaration = _find_declaration(declarations, path, name)
            if declaration is None:
                result.notes.append(
                    f"{path}: symbol {name!r} has no declaration node in the graph (added or "
                    f"removed in the analysed revision, or not parsed); the file seed still "
                    f"represents it")
                continue
            declared = graph.node(declaration)
            if declared is None:
                continue
            heuristic = change.get("attribution") == "git-hunk-header"
            _add_seed(seed_ids, declared,
                      reason=(f"changed {symbol.get('kind') or 'definition'}"
                              + (" (git hunk header, a heuristic)" if heuristic else "")),
                      confidence=0.6 if heuristic else 0.9,
                      origin="inferred" if heuristic else "explicit")
            _add_containing_class(graph, declared, seed_ids)

        # A changed schema or migration declares the entity it writes; the entity is what the
        # rest of the code depends on, so it becomes a seed in its own right.
        for edge in graph.dependencies(node_id):
            target = graph.node(edge.dst)
            if target is not None and target.type == "entity" and edge.relation == "writes":
                _add_seed(seed_ids, target,
                          reason=f"declared by changed file {path}",
                          confidence=edge.confidence, origin=edge.origin)

    result.seeds = list(seed_ids.values())
    visited = set(seed_ids)
    impacted: dict[str, dict[str, Any]] = {}
    frontier = list(seed_ids)
    truncated = False

    for level in range(1, limit + 1):
        next_frontier: list[str] = []
        for node_id in frontier:
            source = graph.node(node_id)
            for edge in graph.dependents(node_id):
                dependent = edge.src
                if dependent in visited:
                    continue
                if len(impacted) >= max_nodes:
                    truncated = True
                    break
                visited.add(dependent)
                node = graph.node(dependent)
                if node is None:
                    continue
                impacted[dependent] = {
                    "node_id": dependent, "type": node.type, "name": node.name,
                    "path": node.path, "depth": level,
                    "via": f"{edge.relation} {source.name if source else node_id}",
                    "relation": edge.relation, "from_node": node_id,
                    "confidence": edge.confidence,
                    "origin": edge.origin,
                    "inferred": edge.origin == "inferred",
                }
                next_frontier.append(dependent)
        if truncated:
            break
        if not next_frontier:
            break
        frontier = next_frontier

    result.impacted = sorted(impacted.values(), key=lambda item: (item["depth"], item["node_id"]))
    result.truncated = truncated
    if truncated:
        result.notes.append(
            f"the walk stopped at the configured limit of {max_nodes} impacted node(s); the list "
            f"is a floor, not the full reach")

    touched: dict[str, dict[str, Any]] = {node_id: seed for node_id, seed in seed_ids.items()}
    touched.update(impacted)
    result.surface = _surface(graph, touched)
    result.workflows, result.journeys = _triggered(graph, touched, result.surface)
    result.tests = _tests(graph, touched, seed_ids)
    result.summary = _summary(result)

    if not seed_ids:
        result.notes.append(
            "no changed file could be located in the dependency graph; reachability factors in "
            "the risk score are therefore zero because nothing could be traced, which is not the "
            "same as low impact")
    if result.missing:
        result.notes.append(
            f"{len(result.missing)} changed path(s) are not represented in the graph; see "
            f"'not represented' for the reason recorded per path")
    inferred_seeds = sum(1 for seed in result.seeds if seed["origin"] == "inferred")
    if inferred_seeds:
        result.notes.append(
            f"{inferred_seeds} seed(s) came from a heuristic rather than a parser (git hunk "
            f"header or a naming convention) and are labelled inferred")
    return result


def _add_seed(seeds: dict[str, dict[str, Any]], node: model.Node, *, reason: str,
              confidence: float = 1.0, origin: str = "explicit") -> None:
    existing = seeds.get(node.node_id)
    if existing is not None:
        if confidence > existing["confidence"]:
            existing.update(confidence=confidence, origin=origin, reason=reason)
        return
    seeds[node.node_id] = {
        "node_id": node.node_id, "type": node.type, "name": node.name, "path": node.path,
        "reason": reason, "confidence": confidence, "origin": origin,
        "inferred": origin == "inferred",
    }


def _declaration_index(graph: model.Graph) -> tuple[dict[tuple[str, str], str],
                                                    dict[tuple[str, str], list[str]]]:
    """(path, qualified name) -> node id, plus a short-name index for qualified lookups."""
    exact: dict[tuple[str, str], str] = {}
    short: dict[tuple[str, str], list[str]] = {}
    for node in graph.nodes.values():
        if node.type in ("class", "symbol", "service") and node.path:
            exact.setdefault((node.path, node.name), node.node_id)
            short.setdefault((node.path, node.name.rsplit(".", 1)[-1]), []).append(node.node_id)
    return exact, short


def _find_declaration(index: tuple[dict[tuple[str, str], str],
                                   dict[tuple[str, str], list[str]]],
                      path: str, name: str) -> str | None:
    exact, short = index
    found = exact.get((path, name))
    if found:
        return found
    matches = short.get((path, name.rsplit(".", 1)[-1]), [])
    # One name may exist both as `Store.write` and as a nested helper `write`; only an
    # unambiguous match is accepted, because the attribution named a qualified definition.
    return matches[0] if len(matches) == 1 else None


def _add_containing_class(graph: model.Graph, declared: model.Node,
                          seeds: dict[str, dict[str, Any]]) -> None:
    if declared.type != "symbol" or not declared.path or "." not in declared.name:
        return
    owner = declared.name.rsplit(".", 1)[0]
    while owner:
        candidate = graph.node(model.class_id(declared.path, owner)) or \
            graph.node(model.service_id(declared.path, owner))
        if candidate is not None:
            _add_seed(seeds, candidate, reason=f"containing class of changed method "
                                               f"{declared.name}", confidence=0.8,
                      origin="inferred")
            return
        if "." not in owner:
            return
        owner = owner.rsplit(".", 1)[0]


def _surface(graph: model.Graph,
             touched: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Public interfaces a touched node depends on: the API it exposes, the entity it uses."""
    found: dict[str, dict[str, Any]] = {}
    for node_id, info in touched.items():
        source = graph.node(node_id)
        if source is None or source.type not in _SOURCE_TYPES:
            continue
        depth = info.get("depth", 0)
        for edge in graph.dependencies(node_id):
            target = graph.node(edge.dst)
            if target is None or target.type not in SURFACE_TYPES:
                continue
            candidate = {
                "node_id": target.node_id, "type": target.type, "name": target.name,
                "path": target.path, "depth": depth,
                "via": f"{source.name} {edge.relation} {target.name}",
                "relation": edge.relation, "from_node": node_id,
                "confidence": edge.confidence, "origin": edge.origin,
                "inferred": edge.origin == "inferred",
            }
            _prefer(found, candidate)
    # The data an endpoint reads or writes is part of its surface even though it is a dependency
    # of the API node rather than of the changed file.
    for item in list(found.values()):
        if item["type"] != "api":
            continue
        for edge in graph.dependencies(item["node_id"]):
            if edge.relation not in ("reads", "writes"):
                continue
            target = graph.node(edge.dst)
            if target is None or target.type != "entity":
                continue
            _prefer(found, {
                "node_id": target.node_id, "type": target.type, "name": target.name,
                "path": target.path, "depth": item["depth"],
                "via": f"{item['name']} {edge.relation} {target.name}",
                "relation": edge.relation, "from_node": item["node_id"],
                "confidence": edge.confidence, "origin": edge.origin,
                "inferred": edge.origin == "inferred",
            })
    return sorted(found.values(), key=lambda item: (item["type"], item["depth"], item["node_id"]))


def _prefer(found: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    """Keep the shallowest path to a node, preferring the strongest edge on a tie."""
    existing = found.get(candidate["node_id"])
    if existing is None or candidate["depth"] < existing["depth"] or (
            candidate["depth"] == existing["depth"]
            and candidate["confidence"] > existing["confidence"]):
        found[candidate["node_id"]] = candidate


def _triggered(graph: model.Graph, touched: dict[str, dict[str, Any]],
               surface: list[dict[str, Any]]
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Workflows and journeys that call a touched or surfaced endpoint."""
    api_depths: dict[str, int] = {}
    for node_id, info in touched.items():
        node = graph.node(node_id)
        if node is not None and node.type == "api":
            api_depths.setdefault(node_id, int(info.get("depth") or 0))
    for item in surface:
        if item["type"] == "api":
            api_depths.setdefault(item["node_id"], int(item.get("depth") or 0))
    reached: dict[str, set[str]] = {}
    for node_id in touched:
        node = graph.node(node_id)
        if node is not None and node.type in ("workflow", "journey"):
            reached.setdefault(node_id, set())
    for api_id, _depth in api_depths.items():
        for edge in graph.dependents(api_id):
            if edge.relation == "triggers":
                reached.setdefault(edge.src, set()).add(api_id)
    workflows: list[dict[str, Any]] = []
    journeys: list[dict[str, Any]] = []
    for node_id in sorted(reached):
        node = graph.node(node_id)
        if node is None:
            continue
        info = touched.get(node_id) or {}
        depth = info.get("depth")
        if depth is None:
            depth = min((api_depths.get(api_id, 0) for api_id in reached[node_id]), default=None)
        entry = {"node_id": node_id, "type": node.type, "name": node.name,
                 "depth": (depth + 1) if isinstance(depth, int) else None,
                 "origin": node.origin, "inferred": node.origin == "inferred",
                 "triggered_from": sorted(reached[node_id])}
        (journeys if node.type == "journey" else workflows).append(entry)
    return workflows, journeys


def _tests(graph: model.Graph, touched: dict[str, dict[str, Any]],
           seeds: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Tests that reach a touched node, plus a coverage note for changed source files."""
    tests: dict[str, dict[str, Any]] = {}
    for node in graph.by_type("test"):
        covered = sorted(edge.dst for edge in graph.dependencies(node.node_id)
                         if edge.dst in touched)
        if node.node_id in touched:
            covered = sorted(set(covered) | {node.node_id})
        if not covered:
            continue
        tests[node.node_id] = {"node_id": node.node_id, "path": node.path, "name": node.name,
                               "covers": covered}
    return sorted(tests.values(), key=lambda item: item["node_id"])


def _summary(result: ImpactResult) -> dict[str, Any]:
    types: dict[str, int] = {}
    for item in result.impacted:
        types[item["type"]] = types.get(item["type"], 0) + 1
    inferred = sum(1 for item in result.impacted if item["inferred"])
    return {
        "seeds": len(result.seeds),
        "impacted": len(result.impacted),
        "impacted_by_type": dict(sorted(types.items())),
        "inferred_impacted": inferred,
        "surface": {kind: sum(1 for item in result.surface if item["type"] == kind)
                    for kind in SURFACE_TYPES},
        "workflows": len(result.workflows),
        "journeys": len(result.journeys),
        "tests": len(result.tests),
        "missing": len(result.missing),
        "max_depth_reached": max((item["depth"] for item in result.impacted), default=0),
        "truncated": result.truncated,
    }
