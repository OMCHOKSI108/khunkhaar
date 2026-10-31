"""The dependency graph: what extraction proves, what resolution infers, how it is stored.

The assertions are about provenance as much as edges. A parser-stated relationship must be
`explicit`; a naming convention, an ORM table name guessed from a base class, or a Phase 2 link
must be `inferred` with a confidence. The `.env` file in the fixture exists to prove that a
configuration file is recorded by presence and its value never reaches the graph.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.impact import graph as impact_graph
from core.impact import model, traverse
from core.metadata import store


def edge(graph, src: str, dst: str, relation: str):
    return next((item for item in graph.dependencies(src)
                 if item.dst == dst and item.relation == relation), None)


@pytest.fixture
def built(impact_repo, kh_root):
    store.init()
    return impact_graph.build("impact-graph-test", impact_repo.root)


def test_python_chain_has_explicit_and_inferred_provenance(built):
    graph = built["graph"]
    item_class = model.class_id("app/models.py", "Item")
    entity = model.entity_id("items")
    assert graph.has(item_class) and graph.has(entity)
    implements = edge(graph, item_class, entity, "implements")
    assert implements is not None
    assert implements.origin == "explicit"
    assert implements.confidence >= 0.8, "__tablename__ is a direct statement, not a guess"

    handler = model.symbol_id("app/main.py", "create_item")
    api = model.api_id("POST", "/items")
    exposed = edge(graph, handler, api, "exposes")
    assert exposed is not None and exposed.origin == "explicit"

    service = model.file_id("app/service.py")
    assert edge(graph, model.file_id("app/main.py"), service, "imports") is not None
    assert edge(graph, service, model.file_id("app/models.py"), "imports") is not None

    create = model.symbol_id("app/service.py", "ItemService.create")
    reads = edge(graph, create, entity, "reads")
    assert reads is not None and reads.origin == "inferred", "ORM use is a heuristic, not a fact"

    # The endpoint reaches the entity through its handler's call into the service.
    api_reads = edge(graph, api, entity, "reads")
    assert api_reads is not None and api_reads.origin == "inferred"
    assert "calls ItemService" in api_reads.evidence

    assert edge(graph, model.test_id("tests/test_service.py"), service, "tests") is not None
    assert graph.resolution["links"]["endpoints_seen"] >= 2
    workflow = model.workflow_id("items")
    assert graph.has(workflow)
    assert any(item.relation == "triggers" for item in graph.dependencies(workflow))


def test_typescript_imports_calls_and_nest_route(built):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    graph = built["graph"]

    client = model.file_id("web/client.ts")
    helper_file = model.file_id("web/helper.ts")
    imported = edge(graph, client, helper_file, "imports")
    assert imported is not None and imported.origin == "explicit"

    run = model.symbol_id("web/client.ts", "run")
    helper = model.symbol_id("web/helper.ts", "helper")
    called = edge(graph, run, helper, "calls")
    assert called is not None and called.origin == "explicit"

    api = model.api_id("GET", "/users/:id")
    assert api == "api:GET /users/{}", "one path parameter is one node, however it is spelled"
    assert graph.has(api)
    handler = model.symbol_id("web/users.controller.ts", "UsersController.findOne")
    exposed = edge(graph, handler, api, "exposes")
    assert exposed is not None and exposed.origin == "explicit"


def test_files_without_a_parser_are_parsed_false_not_empty(built):
    graph = built["graph"]
    ruby = graph.node(model.file_id("legacy/tool.rb"))
    assert ruby is not None
    assert ruby.meta.get("parsed") is False
    assert "no extractor" in str(ruby.meta.get("reason"))
    assert graph.dependencies(ruby.node_id) == [], "an unknown language gets no invented edges"
    assert any("no parser here" in note for note in graph.notes)


def test_environment_file_is_presence_only(impact_repo, built, kh_root):
    graph = built["graph"]
    env = graph.node(model.config_id(".env"))
    assert env is not None
    assert env.meta.get("values_read") is False
    artifact = kh_root / built["artifact"]
    text = artifact.read_text(encoding="utf-8")
    assert "smoke-value-not-a-real-secret" not in text


def test_graph_is_stored_scoped_and_readable(built, impact_repo, kh_root):
    graph = built["graph"]
    info = store.impact_graph_info("impact-graph-test")
    assert info["nodes"] == len(graph.nodes) > 0
    assert info["edges"] == len(graph.edges) > 0

    artifact = kh_root / "intelligence" / "dependency-graphs" / "impact-graph-test.json"
    assert artifact.is_file() and artifact == Path(built["artifact"])
    assert artifact.stat().st_mode & 0o777 == 0o600

    stored = json.loads(artifact.read_text(encoding="utf-8"))
    assert stored["project_id"] == "impact-graph-test"
    assert stored["stats"]["nodes"] == len(graph.nodes)

    rows = store.load_impact_graph("impact-graph-test")
    assert rows["nodes"], "the rows are the queryable copy"
    assert all(not row["node_id"].startswith("impact-graph-test|") for row in rows["nodes"]), (
        "loading restores local ids")
    with store.connect() as conn:
        raw = conn.execute("SELECT node_id FROM impact_nodes WHERE project_id=?",
                           ("impact-graph-test",)).fetchall()
    assert raw and all(row["node_id"].startswith("impact-graph-test|") for row in raw), (
        "the primary key carries the project, so two repositories with the same relative path "
        "cannot collide")


def test_two_projects_never_share_graph_rows(impact_repo, kh_root):
    store.init()
    first = impact_graph.build("project-a", impact_repo.root)
    second = impact_graph.build("project-b", impact_repo.root)
    assert first["stored"]["nodes"] == second["stored"]["nodes"]

    info_a = store.impact_graph_info("project-a")
    info_b = store.impact_graph_info("project-b")
    assert info_a["nodes"] == info_b["nodes"] == first["stored"]["nodes"]

    before = store.load_impact_graph("project-a")
    impact_graph.build("project-b", impact_repo.root)  # rebuilding b must not touch a
    assert store.load_impact_graph("project-a") == before

    local = store.load_impact_graph("project-a")["nodes"]
    assert local and all(not row["node_id"].startswith("project-a|") for row in local)
    assert impact_graph.graph_path("project-a") != impact_graph.graph_path("project-b")


def test_loading_prefers_the_artifact_and_falls_back_to_rows(built, kh_root):
    graph = built["graph"]
    loaded = impact_graph.load("impact-graph-test")
    assert loaded is not None and len(loaded) == len(graph)
    assert loaded.generated_at == graph.generated_at
    assert any("environment file" in note for note in loaded.notes)

    target = impact_graph.graph_path("impact-graph-test")
    target.unlink()
    fallback = impact_graph.load("impact-graph-test")
    assert fallback is not None and len(fallback) == len(graph)
    assert any("metadata store" in note for note in fallback.notes)


def test_reverse_walk_surfaces_api_entity_workflow_and_tests(built):
    graph = built["graph"]
    changeset = {
        "kind": "range", "base": "HEAD~1", "head": "HEAD",
        "files": [{
            "path": "app/service.py", "status": "modified", "insertions": 4, "deletions": 0,
            "hunks": 1, "attribution": "python-ast",
            "symbols": [{"name": "ItemService.rename", "kind": "method", "change": "added"}],
        }],
    }
    result = traverse.analyze(graph, project_id="impact-graph-test", changeset=changeset, depth=4)
    seed_ids = {seed["node_id"] for seed in result.seeds}
    assert model.file_id("app/service.py") in seed_ids
    assert model.class_id("app/service.py", "ItemService") in seed_ids, (
        "a changed method also represents its containing class")
    assert model.symbol_id("app/service.py", "ItemService.rename") in seed_ids

    impacted = {item["node_id"]: item for item in result.impacted}
    assert impacted[model.file_id("app/main.py")]["depth"] == 1
    assert model.test_id("tests/test_service.py") in impacted

    surface = {(item["type"], item["node_id"]) for item in result.surface}
    assert ("api", model.api_id("POST", "/items")) in surface
    assert ("entity", model.entity_id("items")) in surface
    assert {item["name"] for item in result.workflows} == {"items"}
    assert [item["path"] for item in result.tests] == ["tests/test_service.py"]
    assert result.summary["seeds"] == len(result.seeds) >= 3
    assert result.summary["impacted"] == len(result.impacted)


def test_missing_and_deleted_paths_are_reported_not_ignored(built):
    graph = built["graph"]
    changeset = {"files": [
        {"path": "app/deleted.py", "status": "deleted"},
        {"path": "app/never_scanned.go", "status": "added"},
    ]}
    result = traverse.analyze(graph, project_id="impact-graph-test", changeset=changeset)
    assert result.seeds == []
    reasons = {item["path"]: item["reason"] for item in result.missing}
    assert "deleted" in reasons["app/deleted.py"]
    assert "not in the scanned graph" in reasons["app/never_scanned.go"]
    assert any("not represented" in note for note in result.notes)
    assert any("nothing could be traced" in note for note in result.notes)


def test_unparsed_changed_file_is_seeded_but_labelled(built):
    graph = built["graph"]
    changeset = {"files": [{"path": "legacy/tool.rb", "status": "modified"}]}
    result = traverse.analyze(graph, project_id="impact-graph-test", changeset=changeset)
    assert [seed["node_id"] for seed in result.seeds] == [model.file_id("legacy/tool.rb")]
    assert any("legacy/tool.rb" in note and "inferred" in note for note in result.notes)


def test_the_walk_stops_at_the_node_limit_and_says_so(built):
    graph = built["graph"]
    changeset = {"files": [{"path": "app/models.py", "status": "modified"}]}
    result = traverse.analyze(graph, project_id="impact-graph-test", changeset=changeset,
                              max_nodes=1)
    assert result.truncated is True
    assert len(result.impacted) == 1
    assert any("stopped at the configured limit" in note for note in result.notes)
