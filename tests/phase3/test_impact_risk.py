"""The risk score as an explanation: six factors, visible signals, and a sum that adds up.

Nothing here asserts that a number is "correct" - there is no ground truth for prioritisation.
The tests assert the properties the operator relies on: the printed contributions sum to the
printed score, every factor says what raised it, a graph with no reachable nodes scores zero on
reachability (not "low risk"), and the mandatory wording that calls the score a signal travels
with every result.
"""

from __future__ import annotations

import pytest

from core.impact import model, risk, traverse

CHANGESET = {
    "kind": "range", "base": "a", "head": "b",
    "summary": {"files_changed": 1, "insertions": 12, "deletions": 3, "renames": 0,
                "removed": 0},
    "files": [{"path": "app/service.py", "status": "modified", "hunks": 2,
               "insertions": 12, "deletions": 3}],
}


def _graph(with_test: bool = True) -> model.Graph:
    graph = model.Graph(project_id="p", root="/app",
                        generated_at="2026-01-01T00:00:00+00:00")
    nodes = [
        ("file:app/models.py", "file", "app/models.py", "app/models.py", {"parsed": True}),
        ("entity:items", "entity", "items", None, {}),
        ("class:app/models.py:Item", "class", "Item", "app/models.py", {}),
        ("file:app/service.py", "file", "app/service.py", "app/service.py", {"parsed": True}),
        ("symbol:app/service.py:ItemService.create", "symbol", "ItemService.create",
         "app/service.py", {}),
        ("file:app/main.py", "file", "app/main.py", "app/main.py", {"parsed": True}),
        ("symbol:app/main.py:create_item", "symbol", "create_item", "app/main.py", {}),
        ("api:POST /items", "api", "POST /items", None, {}),
        ("workflow:checkout", "workflow", "checkout", None, {}),
        ("journey:J1", "journey", "J1", None, {}),
    ]
    for node_id, kind, name, path, meta in nodes:
        graph.add_node(model.Node(node_id, kind, name, path, meta))
    if with_test:
        graph.add_node(model.Node("test:tests/test_service.py", "test", "tests/test_service.py",
                                  "tests/test_service.py", {"parsed": True}))
    edges = [
        ("class:app/models.py:Item", "entity:items", "implements", 0.85, "explicit"),
        ("file:app/service.py", "file:app/models.py", "imports", 1.0, "explicit"),
        ("symbol:app/service.py:ItemService.create", "entity:items", "reads", 0.6, "inferred"),
        ("file:app/main.py", "file:app/service.py", "imports", 1.0, "explicit"),
        ("symbol:app/main.py:create_item", "symbol:app/service.py:ItemService.create", "calls",
         0.9, "explicit"),
        ("symbol:app/main.py:create_item", "api:POST /items", "exposes", 0.95, "explicit"),
        ("file:app/main.py", "api:POST /items", "exposes", 0.8, "explicit"),
        ("api:POST /items", "entity:items", "reads", 0.45, "inferred"),
        ("workflow:checkout", "api:POST /items", "triggers", 0.6, "inferred"),
        ("journey:J1", "api:POST /items", "triggers", 0.95, "explicit"),
    ]
    if with_test:
        edges += [
            ("test:tests/test_service.py", "file:app/service.py", "imports", 1.0, "explicit"),
            ("test:tests/test_service.py", "file:app/service.py", "tests", 0.85, "explicit"),
        ]
    for src, dst, relation, confidence, origin in edges:
        graph.add_edge(src, dst, relation, confidence=confidence, origin=origin,
                       evidence="fixture")
    return graph


def _impact(graph: model.Graph, changeset: dict | None = None, **kwargs):
    return traverse.analyze(graph, project_id="p", changeset=changeset or CHANGESET, **kwargs)


def _factor(score: dict, name: str) -> dict:
    return next(item for item in score["factors"] if item["name"] == name)


def test_contributions_sum_to_the_printed_score():
    graph = _graph()
    score = risk.score(graph=graph, impact=_impact(graph), changeset=CHANGESET)
    assert len(score["factors"]) == 6
    assert set(risk.WEIGHTS) == {item["name"] for item in score["factors"]}
    assert round(sum(risk.WEIGHTS.values()), 6) == 1.0
    assert round(sum(item["contribution"] for item in score["factors"]), 2) == score["score"]
    assert score["wording"] == risk.WORDING
    assert "approximate prioritisation signal, not a proven probability" in score["wording"]
    assert score["band_note"] == "a bucketed restatement of the same score, not a second estimate"
    for item in score["factors"]:
        assert 0.0 <= item["normalized"] <= 1.0
        assert isinstance(item["signals"], dict) and item["signals"]
        assert item["direction"], "a factor without an explanation is not reviewable"


def test_every_factor_reports_the_graph_it_saw():
    graph = _graph()
    result = _impact(graph)
    score = risk.score(graph=graph, impact=result, changeset=CHANGESET)

    complexity = _factor(score, "change complexity")["signals"]
    assert complexity["files_changed"] == 1
    assert complexity["hunks"] == 2

    dependency = _factor(score, "dependency impact")["signals"]
    assert dependency["impacted_nodes"] == result.summary["impacted"]
    assert dependency["truncated"] is False

    business = _factor(score, "business criticality")["signals"]
    assert business["endpoints_surfaced"] >= 1
    assert business["workflows_reached"] == 1
    assert business["journeys_reached"] == 1

    assert score["inputs"]["seeds"] == len(result.seeds)


def test_a_change_nothing_depends_on_scores_zero_on_reachability():
    graph = _graph()
    changeset = {"kind": "range", "summary": {"files_changed": 1, "insertions": 1},
                 "files": [{"path": "app/missing.py", "status": "modified", "hunks": 1}]}
    result = _impact(graph, changeset=changeset)
    score = risk.score(graph=graph, impact=result, changeset=changeset)
    assert result.seeds == []
    assert _factor(score, "dependency impact")["normalized"] == 0.0
    assert _factor(score, "dependency impact")["contribution"] == 0.0
    assert _factor(score, "business criticality")["normalized"] == 0.0
    assert any("could be traced" in note for note in score["notes"])
    assert any("nothing could be traced" in note for note in result.notes), (
        "the report and the score must say the same thing about an empty walk")


def test_history_counts_statuses_and_ignores_false_positives():
    graph = _graph()
    result = _impact(graph)
    findings = [
        {"id": "F1", "status": "confirmed", "category": "security",
         "source_refs": ["app/service.py:12"]},
        {"id": "F2", "status": "false_positive", "category": "security",
         "source_refs": ["app/service.py:20"]},
        {"id": "F3", "status": "confirmed", "category": "functional",
         "source_refs": ["app/elsewhere.py:1"]},
    ]
    score = risk.score(graph=graph, impact=result, changeset=CHANGESET, findings=findings)
    history = _factor(score, "historical bug density")
    assert history["signals"]["matched_findings"] == 1
    assert history["signals"]["confirmed_matches"] == 1
    assert "F2" not in str(history["signals"]["examples"])
    assert history["normalized"] == pytest.approx(0.25)  # one confirmed match of four


def test_security_sensitivity_comes_from_signals_not_from_the_score():
    graph = _graph()
    result = _impact(graph)
    baseline = risk.score(graph=graph, impact=result, changeset=CHANGESET)
    assert _factor(baseline, "security sensitivity")["normalized"] == 0.0
    assert _factor(baseline, "security sensitivity")["signals"]["auth_signals_in_app_map"] == 0

    with_auth = risk.score(graph=graph, impact=result, changeset=CHANGESET,
                           app_map={"auth_signals": ["Depends(get_current_user)"]})
    assert _factor(with_auth, "security sensitivity")["normalized"] == pytest.approx(0.4)

    auth_changeset = dict(CHANGESET, files=[{"path": "app/auth/login.py", "status": "modified",
                                             "hunks": 1}])
    result_auth = _impact(graph, changeset=auth_changeset)
    both = risk.score(graph=graph, impact=result_auth, changeset=auth_changeset,
                      app_map={"auth_signals": ["jwt"]},
                      findings=[{"id": "F1", "status": "confirmed", "category": "security",
                                 "source_refs": ["app/auth/login.py"]}])
    factor = _factor(both, "security sensitivity")
    assert factor["normalized"] > 0.4
    assert "app/auth/login.py" in factor["signals"]["security_related_paths"]
    assert factor["signals"]["open_security_findings"] == 1


def test_coverage_gap_is_one_without_a_linked_test_and_zero_with_one():
    covered = _graph(with_test=True)
    score = risk.score(graph=covered, impact=_impact(covered), changeset=CHANGESET)
    assert _factor(score, "test coverage gap")["normalized"] == 0.0
    assert _factor(score, "test coverage gap")["signals"]["covered_by_linked_tests"] == 1

    bare = _graph(with_test=False)
    score = risk.score(graph=bare, impact=_impact(bare), changeset=CHANGESET)
    gap = _factor(score, "test coverage gap")
    assert gap["normalized"] == 1.0
    assert gap["signals"]["without_linked_test"] == 1
    assert gap["signals"]["uncovered_examples"] == ["app/service.py"]


def test_missing_history_and_truncation_are_stated():
    graph = _graph()
    truncated = _impact(graph, max_nodes=1)
    score = risk.score(graph=graph, impact=truncated, changeset=CHANGESET)
    assert any("no passed regression run" in note for note in score["notes"])
    assert any("truncated" in note for note in score["notes"])
    assert _factor(score, "dependency impact")["signals"]["truncated"] is True
    assert _factor(score, "dependency impact")["normalized"] >= 0.8

    last_green = {"run_id": "RR-1", "result": "pass"}
    with_green = risk.score(graph=graph, impact=_impact(graph), changeset=CHANGESET,
                            last_green=last_green)
    assert with_green["inputs"]["last_green_regression_run"] == "RR-1"
    assert not any("no passed regression run" in note for note in with_green["notes"])


def test_scores_never_exceed_the_scale_or_go_below_zero():
    graph = _graph()
    huge = {"kind": "range",
            "summary": {"files_changed": 400, "insertions": 90_000, "deletions": 10_000,
                        "renames": 20, "removed": 3},
            "files": [{"path": "app/auth/login.py", "status": "modified", "hunks": 500}]}
    score = risk.score(graph=graph, impact=_impact(graph, changeset=huge), changeset=huge,
                       app_map={"auth_signals": ["jwt", "session", "csrf"]},
                       findings=[{"id": f"F{n}", "status": "confirmed", "category": "security",
                                  "source_refs": ["app/auth/login.py"]} for n in range(20)])
    assert 0.0 <= score["score"] <= 100.0
    assert all(0.0 <= item["normalized"] <= 1.0 for item in score["factors"])
