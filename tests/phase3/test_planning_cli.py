"""`khunkhaar plan` and `khunkhaar qa` as shipped commands.

The CLI owns the approval gate, the plan lifecycle and the run bookkeeping. Execution itself is
replaced by a recording orchestrator so these tests assert what the command hands to the engine,
not what the engine does with it (that is covered by test_planning.py).
"""

from __future__ import annotations

import json
import stat

import pytest

from core import cli
from core.commands import qa as qa_command
from core.metadata import store
from core.planning import planner
from core.project_discovery import detector
from core.regression import generate
from core.runtime.budget import BudgetExceeded

import p4_helpers

TARGET = "http://127.0.0.1:8740"
EXTERNAL = "http://10.20.30.40:8080"


def _analyze(repo) -> str:
    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo.root)]) == 0
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(repo.root),
                     "--base", repo.base_sha, "--head", "HEAD"]) == 0
    return detector.project_id_for(repo.root)


def _with_regression_test(project_id: str) -> None:
    finding = p4_helpers.add_finding(project_id, title="Invalid values are accepted",
                                     source_refs=("app/service.py:12",))
    generate.generate(finding, project_id=project_id)


def _build_plan(repo, captures, *flags: str) -> dict:
    rc = cli.main(["khunkhaar", "plan", "--path", str(repo.root), *flags, "--json"])
    payload = json.loads(captures.readouterr().out)
    assert rc == 0
    return payload


def _runs(project_id: str) -> int:
    with store.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM qa_runs WHERE project_id=?",
                            (project_id,)).fetchone()["n"]


def _fake_orchestrator(instances: list, *, run_status: str = "completed", failure=None):
    class RecordingOrchestrator:
        def __init__(self, options):
            self.options = options
            self.summary = {}
            instances.append(self)

        async def run(self):
            project_id = detector.project_id_for(self.options.root)
            store.start_run(self.options.run_id, project_id, "phase3:qa-plan")
            store.finish_run(self.options.run_id, run_status, "fake execution")
            self.summary = {
                "run_id": self.options.run_id, "plan_id": self.options.plan_id,
                "missions": {"planned": len(self.options.planned_missions or []),
                             "executed": len(self.options.planned_missions or []),
                             "skipped": 0, "failed": 0, "skipped_detail": []},
                "findings": {"created": 0, "updated": 0},
                "resources": {"peak_rss_kb": 1, "llm_usage": "none"},
            }
            if failure is not None:
                raise failure
            return self.summary

    return RecordingOrchestrator


def test_plan_cli_builds_shows_approves_and_lists(kh_root, impact_repo, capsys):
    _analyze(impact_repo)
    capsys.readouterr()
    payload = _build_plan(impact_repo, capsys, "--target", TARGET)

    plan_id = payload["plan_id"]
    artifact = kh_root / payload["artifact"]
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    stored = store.get_qa_plan(plan_id)
    assert stored["status"] == "draft"
    assert stored["digest"] == payload["digest"]

    rc = cli.main(["khunkhaar", "plan", "show", "--plan", plan_id, "--json"])
    shown = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert shown["digest"] == payload["digest"]
    assert shown["plan_id"] == plan_id

    rc = cli.main(["khunkhaar", "plan", "approve", "--plan", plan_id, "--by", "tester",
                   "--json"])
    approved = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert approved["status"] == "approved"
    assert approved["approved_at"] and approved["approved_by"] == "tester"
    assert store.get_qa_plan(plan_id)["approved_by"] == "tester"

    rc = cli.main(["khunkhaar", "plan", "list", "--path", str(impact_repo.root), "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [row["plan_id"] for row in listed["plans"]] == [plan_id]


def test_plan_cli_refuses_an_unregistered_repository(kh_root, impact_repo, capsys):
    rc = cli.main(["khunkhaar", "plan", "--path", str(impact_repo.root)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "not a registered project" in out
    assert store.list_projects() == []


def test_qa_refuses_a_plan_edited_after_review(kh_root, impact_repo, capsys):
    project_id = _analyze(impact_repo)
    capsys.readouterr()
    payload = _build_plan(impact_repo, capsys, "--target", TARGET)
    artifact = kh_root / payload["artifact"]
    edited = json.loads(artifact.read_text(encoding="utf-8"))
    edited["actions"][0]["target"] = "http://127.0.0.1:9999"
    artifact.write_text(json.dumps(edited), encoding="utf-8")

    rc = cli.main(["khunkhaar", "qa", "--plan", payload["plan_id"]])
    out = capsys.readouterr().out
    assert rc == 2
    assert "plan drifted" in out
    assert _runs(project_id) == 0


def test_qa_executes_exactly_the_stored_plan_and_refuses_reuse(
        kh_root, impact_repo, capsys, monkeypatch):
    project_id = _analyze(impact_repo)
    _with_regression_test(project_id)
    capsys.readouterr()
    payload = _build_plan(impact_repo, capsys, "--target", TARGET)
    document, _ = planner.load(payload["plan_id"])
    instances: list = []
    monkeypatch.setattr(qa_command, "Orchestrator", _fake_orchestrator(instances))

    rc = cli.main(["khunkhaar", "qa", "--plan", payload["plan_id"], "--json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(instances) == 1
    options = instances[0].options

    assert options.plan_id == payload["plan_id"]
    assert options.target == document["target"]
    assert options.mode == document["mode"]
    assert [m["mission_id"] for m in options.planned_missions] == [
        m["mission_id"] for m in planner.plan_missions(document)], (
        "execution runs the plan's missions, not a fresh generation")
    planned_tests = planner.plan_regression(document) or {}
    assert [t["test_id"] for t in options.planned_regression["selected_tests"]] == [
        t["test_id"] for t in planned_tests["selected_tests"]]

    stored = store.get_qa_plan(payload["plan_id"])
    assert stored["status"] == "completed"
    assert stored["run_id"] == options.run_id
    assert result["plan_status"] == "completed"
    assert store.get_run(options.run_id)["status"] == "completed"

    rc = cli.main(["khunkhaar", "qa", "--plan", payload["plan_id"]])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already ran" in out
    assert _runs(project_id) == 1


def test_qa_requires_and_records_approval_for_an_external_target(
        kh_root, impact_repo, capsys, monkeypatch, patch_config):
    project_id = _analyze(impact_repo)
    patch_config("api", {"allowed_hosts": ["127.0.0.1:8740", "10.20.30.40:8080"]})
    store.grant_authorization(project_id, EXTERNAL, "controlled", granted_by="tester")

    document = planner.build(project_id, impact_repo.root, target=EXTERNAL)
    planner.save(document)
    plan_id = document["plan_id"]
    capsys.readouterr()

    rc = cli.main(["khunkhaar", "qa", "--plan", plan_id])
    out = capsys.readouterr().out
    assert rc == 2
    assert "require approval" in out
    stored = store.get_qa_plan(plan_id)
    assert stored["status"] == "draft" and not stored["approved_at"]
    assert _runs(project_id) == 0

    instances: list = []
    monkeypatch.setattr(qa_command, "Orchestrator", _fake_orchestrator(instances))
    rc = cli.main(["khunkhaar", "qa", "--plan", plan_id, "--approve", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    stored = store.get_qa_plan(plan_id)
    assert stored["status"] == "completed"
    assert stored["approved_at"] and stored["approved_by"]
    assert result["plan_status"] == "completed"
    approved_document, _ = planner.load(plan_id)
    approval = approved_document["approval"]
    assert approval["approved_at"] == stored["approved_at"]
    assert approval["approved_by"] == stored["approved_by"]


def test_qa_impact_only_builds_an_impact_derived_plan_and_executes_it(
        kh_root, impact_repo, capsys, monkeypatch):
    project_id = _analyze(impact_repo)
    _with_regression_test(project_id)
    capsys.readouterr()
    instances: list = []
    monkeypatch.setattr(qa_command, "Orchestrator", _fake_orchestrator(instances))

    rc = cli.main(["khunkhaar", "qa", "--impact-only", "--path", str(impact_repo.root),
                   "--target", TARGET, "--json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    document, _ = planner.load(result["plan_id"])
    assert document["impact_only"] is True
    mission_actions = [a for a in document["actions"] if a["kind"] == "mission"]
    assert mission_actions
    assert all(a["rationale"]["impact"] for a in mission_actions)

    options = instances[0].options
    assert [m["mission_id"] for m in options.planned_missions] == [
        a["mission"]["mission_id"] for a in mission_actions]
    assert store.get_qa_plan(result["plan_id"])["status"] == "completed"


def test_qa_impact_only_without_a_change_set_executes_nothing(
        kh_root, impact_repo, capsys, monkeypatch):
    project_id = p4_helpers.register(impact_repo.root)
    instances: list = []
    monkeypatch.setattr(qa_command, "Orchestrator", _fake_orchestrator(instances))
    capsys.readouterr()

    rc = cli.main(["khunkhaar", "qa", "--impact-only", "--path", str(impact_repo.root),
                   "--target", TARGET, "--json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["executed"] is False
    assert "no mapped mission" in result["note"]
    assert instances == []
    plan = store.list_qa_plans(project_id)[0]
    assert plan["status"] == "draft"
    assert _runs(project_id) == 0


@pytest.mark.parametrize(("kind", "run_status", "plan_status"), [
    ("cancelled", "cancelled", "cancelled"),
    ("steps", "budget_exceeded", "budget_exceeded"),
])
def test_qa_records_cancellation_and_budget_exhaustion_on_the_plan(
        kh_root, impact_repo, capsys, monkeypatch, kind, run_status, plan_status):
    _analyze(impact_repo)
    capsys.readouterr()
    payload = _build_plan(impact_repo, capsys, "--target", TARGET)
    instances: list = []
    failure = BudgetExceeded(kind, 2, 1)
    monkeypatch.setattr(qa_command, "Orchestrator",
                        _fake_orchestrator(instances, run_status=run_status, failure=failure))

    rc = cli.main(["khunkhaar", "qa", "--plan", payload["plan_id"]])
    out = capsys.readouterr().out
    assert rc == 1
    assert "stopped by budget" in out
    stored = store.get_qa_plan(payload["plan_id"])
    run = store.get_run(instances[0].options.run_id)
    assert stored["status"] == plan_status
    assert run["status"] == run_status


def test_qa_requires_exactly_one_mode(kh_root, impact_repo, capsys):
    rc = cli.main(["khunkhaar", "qa"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "exactly one" in out

    rc = cli.main(["khunkhaar", "qa", "--impact-only", "--path", str(impact_repo.root)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "--target" in out
