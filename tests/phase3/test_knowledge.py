"""Module E: project knowledge, the global technique boundary and auditable retrieval.

The knowledge document is a projection of store rows. These tests assert that the projection keeps
the project's facts, drops request payloads and credentials, never lets a project identifier into
the global library, and explains every retrieval weight.
"""

from __future__ import annotations

import importlib
import json
import stat

from core import cli
from core.findings import store as findings_store
from core.knowledge import project as project_knowledge
from core.knowledge import retrieve as retrieve_module
from core.metadata import store
from core.regression import generate

import p4_helpers

global_knowledge = importlib.import_module("core.knowledge.global")


def _seed(pid: str, *, with_regression: bool = False) -> dict:
    security = p4_helpers.add_finding(
        pid, title="A user can read another user's item", category="security",
        severity="high", source_refs=("app/service.py:12",), rule_ids=("R1",),
        expected="a non-owner receives 403/404", actual="HTTP 200 returned the owner's item")
    integrity = p4_helpers.add_finding(
        pid, title="A failed approval must not leave an audit record behind",
        category="functional", severity="medium", source_refs=("app/service.py:30",),
        reproduction=p4_helpers.probe_reproduction("atomicity"))
    likely = p4_helpers.add_finding(
        pid, title="Approval may skip a required step", status="likely", category="functional",
        severity="high", source_refs=("app/workflow.py:9",))
    rejected = p4_helpers.add_finding(
        pid, title="The catalog is public without login", status="false_positive",
        category="security", severity="low", source_refs=("app/service.py:40",),
        note="the catalog is intentionally public; no authorization boundary exists")
    findings_store.add_invariant(pid, {
        "id": "INV-R1", "rule_id": "R1",
        "description": "A user must never read another user's items.",
        "source": "docs/requirements.md:3", "feature": "service", "roles": ["user"],
        "method": "cross-user resource access probe", "confidence": 0.9,
        "entities": ["Item"], "kind": "authorization",
    })
    findings_store.add_invariant(pid, {
        "id": "INV-R2", "rule_id": "R2",
        "description": "Amounts must be positive.",
        "source": "docs/requirements.md:5", "feature": "service", "roles": [],
        "method": "invalid/empty/extreme input probe", "confidence": 0.85,
        "entities": ["Item"], "kind": "validation",
    })
    findings_store.save_journey(pid, "J-1", "approve an item", [
        {"name": "create", "method": "POST", "path": "/items",
         "payload": {"name": "sample"}, "actor": "user", "expect": "2xx"},
        {"name": "approve", "method": "POST", "path": "/items/{id}/approve",
         "payload": {}, "actor": "approver", "expect": "2xx"},
    ])
    findings_store.save_app_map(pid, "intelligence/app-map.json", {
        "entities": [{"name": "Item", "source": "app/models.py", "line": 12}],
        "rules": [{"rule_id": "R1", "kind": "authorization"}],
    })
    test = None
    if with_regression:
        test = generate.generate(security, project_id=pid)["test"]
        store.record_regression_run(pid, test_id=test["test_id"], result="pass",
                                    run_id="run-green", base_ref="main", head_ref="HEAD",
                                    duration_ms=12, note="the defect was not observed",
                                    evidence=None)
    return {"security": security, "integrity": integrity, "likely": likely,
            "rejected": rejected, "test": test}


def test_project_document_projects_the_store_without_payloads(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    seeded = _seed(pid, with_regression=True)

    document = project_knowledge.build(pid)
    assert document["scope"] == "project"
    assert document["project"]["project_id"] == pid

    confirmed_ids = {row["finding_id"] for row in document["confirmed_bugs"]}
    assert {seeded["security"]["id"], seeded["integrity"]["id"]} <= confirmed_ids
    assert seeded["likely"]["id"] not in confirmed_ids
    resolved_ids = {row["finding_id"] for row in document["resolved_bugs"]}
    assert seeded["security"]["id"] in resolved_ids

    patterns = {row["category"]: row for row in document["recurring_patterns"]}
    assert patterns["functional"]["count"] == 2
    assert seeded["likely"]["id"] in patterns["functional"]["finding_ids"]

    rejected = {row["finding_id"]: row for row in document["false_positive_patterns"]}
    assert "intentionally public" in rejected[seeded["rejected"]["id"]]["reason"]

    boundaries = document["permission_boundaries"]
    assert any("never read another user's items" in row["boundary"] for row in boundaries)
    edge_rules = {row["reference"] for row in document["edge_cases"]}
    assert "R2" in edge_rules

    workflow = document["workflows"][0]
    assert workflow["name"] == "approve an item"
    assert all("payload" not in step for step in workflow["steps"])

    assert document["db_relations"]["entities"][0]["name"] == "Item"
    assert document["regression"]["by_result"]["pass"] == 1
    assert any("not proof" in note for note in document["notes"])
    assert any("approximate prioritisation signal" in note for note in document["notes"])
    assert document["risky_modules"]["features"]


def test_writing_knowledge_keeps_the_global_library_free_of_project_data(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    _seed(pid, with_regression=True)

    result = project_knowledge.save(pid)
    assert stat.S_IMODE((kh_root / result["json"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((kh_root / result["markdown"]).stat().st_mode) == 0o600
    item = store.get_knowledge_item(project_knowledge.item_id(pid))
    assert item["scope"] == "project" and item["project_id"] == pid
    assert item["body_path"] == result["json"]
    document = json.loads((kh_root / result["json"]).read_text(encoding="utf-8"))
    assert str(impact_repo.root) not in json.dumps(document)
    assert "payload" not in json.dumps(document["workflows"]).lower()

    techniques = global_knowledge.save()
    assert stat.S_IMODE((kh_root / techniques["json"]).stat().st_mode) == 0o600
    text = (kh_root / techniques["json"]).read_text(encoding="utf-8")
    text += (kh_root / techniques["markdown"]).read_text(encoding="utf-8")
    for forbidden in (pid, "impact-app", "app/service.py", "RULE R", "/home/", str(kh_root),
                      "A user can read another user's item"):
        assert forbidden not in text
    assert store.list_knowledge_items("global")
    assert all(entry["project_id"] is None
               for entry in store.list_knowledge_items("global"))
    assert len(store.list_knowledge_items("global")) == len(global_knowledge.TECHNIQUES)


def test_retrieval_weights_are_auditable_and_integrity_changes_the_order(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    integrity = p4_helpers.add_finding(
        pid, title="A failed approval must not leave an audit record behind",
        category="functional", severity="medium", source_refs=("app/service.py:20",),
        reproduction=p4_helpers.probe_reproduction("atomicity"))
    ordinary = p4_helpers.add_finding(
        pid, title="The totals page rounds down", category="functional", severity="high",
        source_refs=("app/service.py:44",), reproduction=p4_helpers.probe_reproduction("validation"))
    integrity_test = generate.generate(integrity, project_id=pid)["test"]
    ordinary_test = generate.generate(ordinary, project_id=pid)["test"]
    store.update_regression_test(integrity_test["test_id"], reliability="trusted",
                                 validated_path="regression/validated/example.py")

    result = retrieve_module.retrieve(pid, {"files": [{"path": "app/service.py"}]})
    assert result["changeset"]["changed_paths"] == ["app/service.py"]
    assert result["weights"]["transaction_integrity"] == 0.15
    top = result["items"][0]
    assert top["id"] == integrity["id"]
    assert top["weight"] > result["items"][1]["weight"]
    factors = {item["factor"] for item in top["contributions"]}
    assert "transaction_integrity" in factors
    assert "the finding is about failed-operation integrity" in next(
        item["reason"] for item in top["contributions"]
        if item["factor"] == "transaction_integrity")
    for item in result["items"]:
        assert round(sum(part["weight"] for part in item["contributions"]), 2) == item["weight"]
        assert item["contributions"]

    tests = {item["test_id"]: item for item in result["tests"]}
    assert {integrity_test["test_id"], ordinary_test["test_id"]} <= set(tests)
    assert tests[integrity_test["test_id"]]["reliability"] == "trusted"
    assert tests[integrity_test["test_id"]]["weight"] > tests[ordinary_test["test_id"]]["weight"]
    assert result["bias"]["test_order"][0] == integrity_test["test_id"]

    baseline = retrieve_module.retrieve(pid, None)
    assert baseline["changeset"]["supplied"] is False
    assert baseline["changeset"]["changed_paths"] == []
    assert "standalone history" in baseline["changeset"]["note"]


def test_knowledge_cli_builds_shows_and_retrieves(kh_root, impact_repo, capsys):
    pid = p4_helpers.register(impact_repo.root)
    p4_helpers.add_finding(pid, title="Invalid values are accepted",
                           source_refs=("app/service.py:12",))

    assert cli.main(["khunkhaar", "knowledge", "build", "--path", str(impact_repo.root),
                     "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["project_id"] == pid
    assert (kh_root / built["json"]).is_file()
    assert (kh_root / built["global"]["json"]).is_file()

    assert cli.main(["khunkhaar", "knowledge", "show", "--path", str(impact_repo.root),
                     "--section", "confirmed_bugs"]) == 0
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed[0]["title"] == "Invalid values are accepted"

    assert cli.main(["khunkhaar", "knowledge", "retrieve", "--path", str(impact_repo.root),
                     "--changed", "app/service.py", "--json"]) == 0
    retrieved = json.loads(capsys.readouterr().out)
    assert retrieved["items"][0]["title"] == "Invalid values are accepted"
    assert retrieved["items"][0]["weight"] > 0

    assert cli.main(["khunkhaar", "knowledge", "show", "--path", str(impact_repo.root),
                     "--section", "not_a_section"]) == 2
    assert "unknown section" in capsys.readouterr().out
