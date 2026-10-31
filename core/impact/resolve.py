"""Link the extracted graph to the Phase 2 application map, journeys and schema declarations.

Extraction knows what a parser saw. Resolution adds the observations that cross a language or a
tool boundary:

* endpoints from the Phase 2 app map (and its OpenAPI scan) become `api` nodes, linked to the file
  that declares them;
* documented entities that extraction did not already model become `entity` nodes;
* features become `workflow` nodes that trigger the routes their name matches;
* saved journeys become `journey` nodes that trigger the routes their steps call;
* handlers that read or write an entity propagate that relation to the API node they expose, so a
  schema change reaches the endpoint and its workflow;
* a test file whose name matches exactly one source module gains an inferred `tests` edge, clearly
  labelled as a naming convention rather than an import.

Everything added here that a parser did not state is marked `inferred` with a confidence, and any
link that could not be made is recorded in `unresolved` instead of being guessed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.findings import store as findings_store
from core.impact import model
from core.intelligence import mapper, rules


def app_map_for(root: Path, project_id: str) -> dict[str, Any]:
    """The latest persisted app map, or a bounded fresh scan when none exists.

    A persisted map is preferred because it is what the operator's last analysis saw, including
    its business rules. A fresh scan is read-only and never saved here: building a graph must not
    silently replace the app map a finding was recorded against.
    """
    latest = findings_store.latest_app_map(project_id)
    if latest and isinstance(latest.get("summary"), dict) and latest["summary"]:
        summary = dict(latest["summary"])
        summary.setdefault("source", latest.get("path"))
        return summary
    app = mapper.build(root, project_id, rules_classifier=rules.extract_rules)
    return app.to_dict()


def apply(graph: model.Graph, *, root: Path, project_id: str,
          app_map: dict[str, Any] | None = None,
          journeys: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    app_map = app_map if app_map is not None else app_map_for(root, project_id)
    if journeys is None:
        try:
            journeys = findings_store.list_journeys(project_id)
        except Exception:  # a missing findings database must not turn a graph build into a failure
            journeys = []
    paths = graph.path_index()
    unresolved: list[dict[str, Any]] = []
    endpoints = app_map.get("endpoints") or []
    entities = app_map.get("entities") or []
    features = app_map.get("features") or []

    api_nodes = _link_endpoints(graph, endpoints, paths, unresolved)
    _link_entities(graph, entities, paths, unresolved)
    all_apis = graph.by_type("api")
    workflow_links = _link_workflows(graph, features, all_apis)
    journey_links = _link_journeys(graph, journeys, unresolved)
    entity_links = _link_api_entities(graph, all_apis)
    conventions = _link_test_conventions(graph)

    extra = 0
    if len(unresolved) > 200:
        extra = len(unresolved) - 200
        unresolved = unresolved[:200]

    summary = {
        "app_map_source": app_map.get("source") or "in-memory scan",
        "endpoints_seen": len(endpoints),
        "entities_seen": len(entities),
        "api_nodes": len(api_nodes),
        "workflow_links": workflow_links,
        "journey_links": journey_links,
        "api_entity_links": entity_links,
        "test_conventions": conventions,
        "unresolved": unresolved,
        "unresolved_omitted": extra,
    }
    existing = graph.resolution if isinstance(graph.resolution, dict) else {}
    existing["links"] = summary
    graph.resolution = existing
    if unresolved:
        graph.notes.append(
            f"{len(unresolved)} cross-tool link(s) could not be resolved and were recorded as "
            f"unresolved rather than guessed; they are listed under resolution.links.unresolved")
    return summary


def _link_endpoints(graph: model.Graph, endpoints: list[dict[str, Any]], paths: dict[str, str],
                    unresolved: list[dict[str, Any]]) -> list[model.Node]:
    nodes: list[model.Node] = []
    for endpoint in endpoints:
        method = str(endpoint.get("method") or "ANY").upper()
        raw_path = endpoint.get("path") or "/"
        node_id = model.api_id(method, raw_path)
        source = str(endpoint.get("source") or "")
        framework = str(endpoint.get("framework") or "")
        if not graph.has(node_id):
            normalized = model.normalize_path(raw_path)
            graph.add_node(model.Node(
                node_id, "api", f"{method} {normalized}", None,
                {"path": normalized, "method": method, "source": source,
                 "line": endpoint.get("line"), "protocol": "http", "framework": framework,
                 "origin": "phase2-app-map"}, 0.6, "inferred"))
        api = graph.node(node_id)
        if api is not None:
            nodes.append(api)
        source_node = paths.get(source)
        if source_node is None:
            if source:
                unresolved.append({
                    "kind": "api-source", "method": method, "path": raw_path, "source": source,
                    "confidence": 0.3,
                    "reason": "the route's source file is not in the graph's scanned set"})
            continue
        graph.add_edge(
            source_node, node_id, "exposes", confidence=0.6, origin="inferred",
            evidence=f"Phase 2 route scan found {method} {raw_path} in {source}"
                     + (f" (framework {framework})" if framework else ""))
    return nodes


def _link_entities(graph: model.Graph, entities: list[dict[str, Any]], paths: dict[str, str],
                   unresolved: list[dict[str, Any]]) -> None:
    for entity in entities:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        source = str(entity.get("source") or "")
        node_id, mapped = _entity_target(graph, name, source)
        if not graph.has(node_id):
            graph.add_node(model.Node(
                node_id, "entity", name, source,
                {"file": source, "line": entity.get("line"), "declared_as": "phase2-scan",
                 "origin": "phase2-app-map"}, 0.6, "inferred"))
        source_node = paths.get(source)
        if source_node is None:
            if source:
                unresolved.append({
                    "kind": "entity-source", "entity": name, "source": source, "confidence": 0.3,
                    "reason": "the entity's source file is not in the graph's scanned set"})
            continue
        graph.add_edge(
            source_node, node_id, "implements", confidence=0.6 if mapped else 0.55,
            origin="inferred",
            evidence=(f"Phase 2 entity scan named {name} in {source}"
                      + ("; it is the ORM mapping the parser already modelled"
                         if mapped else "")))


def _entity_target(graph: model.Graph, name: str, source: str) -> tuple[str, bool]:
    """The entity a Phase 2 name refers to, preferring an ORM table the parser already mapped.

    Phase 2 names model classes (`Item`); extraction names the table they map to (`items`). Both
    are observations of the same thing, so they must not become two entity nodes that split the
    impact of one schema.
    """
    if source:
        declared = graph.node(model.class_id(source, name))
        if declared is not None:
            for edge in graph.dependencies(declared.node_id):
                if edge.relation != "implements":
                    continue
                target = graph.node(edge.dst)
                if target is not None and target.type == "entity":
                    return target.node_id, True
    return model.entity_id(name), False


def _link_workflows(graph: model.Graph, features: list[str], apis: list[model.Node]) -> int:
    links = 0
    for raw in features:
        feature = str(raw).strip().lower()
        if not feature:
            continue
        node_id = model.workflow_id(feature)
        if not graph.has(node_id):
            graph.add_node(model.Node(node_id, "workflow", feature, None,
                                      {"kind": "feature", "feature": feature}, 0.6, "inferred"))
        for api in apis:
            path = str(api.meta.get("path") or "").lower()
            segments = [segment for segment in path.split("/") if segment and segment != "{}"]
            if feature in segments:
                graph.add_edge(node_id, api.node_id, "triggers", confidence=0.6,
                               origin="inferred",
                               evidence=f"feature {feature!r} is a path segment of {path}")
                links += 1
    return links


def _link_journeys(graph: model.Graph, journeys: list[dict[str, Any]],
                   unresolved: list[dict[str, Any]]) -> int:
    links = 0
    for journey in journeys:
        identifier = str(journey.get("id") or journey.get("journey_id") or "")
        if not identifier:
            continue
        node_id = model.journey_id(identifier)
        graph.add_node(model.Node(node_id, "journey", str(journey.get("name") or identifier),
                                  None, {"journey_id": identifier,
                                         "name": journey.get("name"),
                                         "feature": journey.get("feature")}, 1.0))
        for step in journey.get("steps") or []:
            method = str(step.get("method") or "ANY").upper()
            path = step.get("path") or "/"
            target = model.api_id(method, path)
            if not graph.has(target) and method != "ANY":
                target = model.api_id("ANY", path)
                if not graph.has(target):
                    target = ""
            elif not graph.has(target):
                target = ""
            if target:
                graph.add_edge(node_id, target, "triggers", confidence=0.95,
                               evidence=f"journey {identifier} step "
                                        f"{step.get('name') or '(unnamed)'} calls "
                                        f"{method} {path}")
                links += 1
            else:
                unresolved.append({
                    "kind": "journey-step", "journey": identifier,
                    "step": step.get("name"), "method": method, "path": path,
                    "confidence": 0.4,
                    "reason": "no api node with this method and path exists in the graph"})
    return links


def _link_api_entities(graph: model.Graph, apis: list[model.Node]) -> int:
    """Attach the entities an endpoint reaches, directly or through one service call.

    The direct link is what the handler itself reads or writes. The one-hop link follows the
    handler's call into the service method it invokes, which is how most controllers reach the
    database; it is labelled and carries a lower confidence because the call may take another
    branch at runtime.
    """
    methods = _method_index(graph)
    links = 0
    for api in apis:
        handlers = [edge.src for edge in graph.dependents(api.node_id)
                    if edge.relation == "exposes"]
        for handler in handlers:
            handler_node = graph.node(handler)
            if handler_node is None or handler_node.type not in ("file", "symbol", "class",
                                                                   "service"):
                continue
            for edge in graph.dependencies(handler):
                if edge.relation not in ("reads", "writes"):
                    continue
                target = graph.node(edge.dst)
                if target is None or target.type != "entity":
                    continue
                created = graph.add_edge(
                    api.node_id, edge.dst, edge.relation, confidence=0.55, origin="inferred",
                    evidence=f"handler {handler_node.name} {edge.relation} {target.name}")
                if created is not None and created.src == api.node_id:
                    links += 1
            for call in graph.dependencies(handler):
                if call.relation != "calls":
                    continue
                callee = graph.node(call.dst)
                if callee is None or callee.type == "file":
                    continue
                targets = [callee.node_id]
                if callee.type in ("class", "service") and callee.path:
                    targets = methods.get((callee.path, callee.name), [])
                for target_id in targets:
                    for edge in graph.dependencies(target_id):
                        if edge.relation not in ("reads", "writes"):
                            continue
                        target = graph.node(edge.dst)
                        if target is None or target.type != "entity":
                            continue
                        created = graph.add_edge(
                            api.node_id, edge.dst, edge.relation, confidence=0.45,
                            origin="inferred",
                            evidence=f"handler {handler_node.name} calls {callee.name}, which "
                                     f"{edge.relation} {target.name}")
                        if created is not None and created.src == api.node_id:
                            links += 1
    return links


def _method_index(graph: model.Graph) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], list[str]] = {}
    for node in graph.by_type("symbol"):
        if node.path and "." in node.name:
            owner = node.name.rsplit(".", 1)[0]
            index.setdefault((node.path, owner), []).append(node.node_id)
    return index


_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx",
                  ".spec.js", ".spec.jsx")


def _module_stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    for suffix in _TEST_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    else:
        name = name.rsplit(".", 1)[0] if "." in name else name
    if name.startswith("test_"):
        name = name[5:]
    if name.endswith("_test"):
        name = name[:-5]
    return name.lower()


def _link_test_conventions(graph: model.Graph) -> int:
    sources = {node.path: node for node in graph.by_type("file") if node.path}
    stems: dict[str, list[str]] = {}
    for path in sources:
        stems.setdefault(_module_stem(path), []).append(path)
    links = 0
    for test in graph.by_type("test"):
        if any(edge.relation == "tests" for edge in graph.dependencies(test.node_id)):
            continue
        matches = stems.get(_module_stem(test.path or ""), [])
        if len(matches) != 1:
            continue
        graph.add_edge(test.node_id, sources[matches[0]].node_id, "tests", confidence=0.4,
                       origin="inferred",
                       evidence=f"test module name matches {matches[0]} (a naming convention, "
                                f"not an import)")
        links += 1
    return links
