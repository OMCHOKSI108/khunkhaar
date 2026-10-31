"""Negative acceptance scenarios: nothing may be invented, and nothing may be lost.

These tests exercise the end-to-end commands against repositories and targets that are empty,
dirty, interrupted or unreachable. Each asserts the same two properties from a different angle:
a run may report an infrastructure or environment problem, but it may never turn that problem
into a confirmed application defect, and an interrupted run may never be recorded as complete.

Other negative paths live with the module they belong to rather than being duplicated here:
GitLab outages (`test_mr_cli.py::test_gitlab_unavailable_leaves_the_local_review_identical`),
offline scheduled checks (`test_monitoring.py::test_smoke_transport_error_is_never_a_defect`),
missing browser engines and validation environment failures (`test_monitoring.py`), and the
approval gate itself (`test_planning_cli.py`).
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from core import cli
from core.commands import qa as qa_command
from core.findings import store as findings_store
from core.metadata import store
from core.orchestrator import engine
from core.project_discovery import detector
from core.shared import evidence

import p4_helpers

TARGET = "http://127.0.0.1:8740"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _register_and_analyze(repo) -> str:
    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo.root)]) == 0
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(repo.root),
                     "--base", repo.base_sha, "--head", "HEAD"]) == 0
    return detector.project_id_for(repo.root)


def _build_plan(repo, capsys, *flags: str) -> dict:
    assert cli.main(["khunkhaar", "plan", "--path", str(repo.root), *flags, "--json"]) == 0
    return _json(capsys)


def _runs(project_id: str) -> int:
    with store.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM qa_runs WHERE project_id=?",
                            (project_id,)).fetchone()["n"]


def _findings(project_id: str) -> list[dict]:
    return findings_store.list_findings(project_id)


# ------------------------------------------------------------------ nothing changed


def test_a_repository_with_no_changes_yields_an_empty_plan_and_no_run(kh_root, impact_repo,
                                                                     capsys):
    project_id = p4_helpers.register(impact_repo.root)
    head = impact_repo.sha()
    before = impact_repo.state()

    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(impact_repo.root),
                     "--base", head, "--head", head, "--json"]) == 0
    analyzed = _json(capsys)
    assert analyzed["changeset"]["files"] == []
    assert analyzed["changeset"]["summary"]["files_changed"] == 0

    assert cli.main(["khunkhaar", "impact", "--path", str(impact_repo.root), "--json"]) == 0
    impact = _json(capsys)
    assert impact["impact"]["surface"] == []
    risk = impact["risk"]
    assert 0 <= risk["score"] <= 100
    assert round(sum(item["contribution"] for item in risk["factors"]), 2) == risk["score"]
    assert "not a proven probability" in risk["wording"]

    plan = _build_plan(impact_repo, capsys, "--impact-only", "--target", TARGET)
    assert plan["actions"] == []
    assert plan["summary"]["missions"] == 0
    assert plan["summary"]["regression_tests"] == 0
    assert any("empty plan is reported" in note for note in plan["inputs"]["notes"]), (
        "an empty plan must say why it is empty")

    assert _runs(project_id) == 0, "planning must never start a QA run"
    assert len(_findings(project_id)) == 0
    assert impact_repo.state() == before


# ------------------------------------------------------- dirty and multi-commit histories


def test_multi_commit_rename_delete_and_dirty_state_are_reported(kh_root, dirty_repo, capsys):
    p4_helpers.register(dirty_repo.root)
    before = dirty_repo.state()

    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(dirty_repo.root),
                     "--base", dirty_repo.base_sha,
                     "--head", "feature/payments", "--json"]) == 0
    changeset = _json(capsys)["changeset"]
    by_path = {item["path"]: item for item in changeset["files"]}
    assert by_path["current.txt"]["status"] == "renamed"
    assert by_path["current.txt"]["old_path"] == "legacy.txt"
    assert "gone.txt" not in by_path, "a file added and deleted between the refs is no change"
    assert by_path["bin.dat"]["binary"] is True
    assert len(changeset["commits"]) == 5
    assert changeset["summary"]["worktree_clean"] is False
    assert changeset["index_untouched"] is True

    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(dirty_repo.root),
                     "--working-tree", "--json"]) == 0
    worktree = _json(capsys)["changeset"]
    dirty = {item["path"]: item for item in worktree["files"]}
    assert dirty["src/app.py"]["staged"] and not dirty["src/app.py"]["unstaged"]
    assert dirty["cfg.yaml"]["unstaged"] and not dirty["cfg.yaml"]["staged"]
    assert dirty["scratch.txt"]["untracked"] is True

    assert dirty_repo.state() == before, "analysis never stages, restores or rewrites anything"


# ------------------------------------------------------------------------ interruption


def test_an_interrupted_engine_run_is_recorded_as_interrupted(kh_root, impact_repo, monkeypatch):
    project_id = p4_helpers.register(impact_repo.root)
    run_id = evidence.new_run_id("qa")
    orchestrator = engine.Orchestrator(engine.Options(
        root=impact_repo.root, target=None, db_path=None, users_file=None,
        mode="controlled", verify=False, command="qa", plan_id="PL-interrupt", run_id=run_id))

    async def interrupt(_missions):
        raise KeyboardInterrupt()

    monkeypatch.setattr(orchestrator, "_execute_missions", interrupt)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(orchestrator.run())

    row = store.get_run(run_id)
    assert row is not None and row["status"] == "interrupted", (
        "an interrupted run must never be left looking completed or running")
    assert orchestrator.summary["errors"] == ["interrupted by the operator"]
    run_artifact = kh_root / orchestrator.summary["evidence"]["run"]
    assert run_artifact.is_file()
    assert _findings(project_id) == []


def test_the_qa_command_marks_the_plan_interrupted_and_exits_130(kh_root, impact_repo,
                                                                 capsys, monkeypatch):
    project_id = _register_and_analyze(impact_repo)
    capsys.readouterr()
    plan = _build_plan(impact_repo, capsys, "--target", TARGET)

    instances: list = []

    class InterruptingOrchestrator:
        def __init__(self, options):
            self.options = options
            self.summary = {"run_id": options.run_id, "plan_id": options.plan_id}
            instances.append(self)

        async def run(self):
            store.start_run(self.options.run_id, detector.project_id_for(self.options.root),
                            "phase3:qa-plan")
            raise KeyboardInterrupt()

    monkeypatch.setattr(qa_command, "Orchestrator", InterruptingOrchestrator)
    rc = cli.main(["khunkhaar", "qa", "--plan", plan["plan_id"], "--approve", "--json"])
    payload = _json(capsys)

    assert rc == 130
    assert payload["plan_status"] == "interrupted"
    assert store.get_qa_plan(plan["plan_id"])["status"] == "interrupted"
    assert len(instances) == 1
    run = store.get_run(instances[0].options.run_id)
    assert run is not None and run["status"] == "interrupted"
    assert _runs(project_id) == 1
    assert _findings(project_id) == []


# ------------------------------------------------------- unreachable environment / no DB


def test_an_unreachable_target_and_missing_database_are_infrastructure_errors(
        kh_root, impact_repo, capsys, patch_config):
    port = _closed_port()
    target = f"http://127.0.0.1:{port}"
    patch_config("api", {"allowed_hosts": ["127.0.0.1:8731", f"127.0.0.1:{port}"]})
    project_id = _register_and_analyze(impact_repo)
    capsys.readouterr()
    plan = _build_plan(impact_repo, capsys, "--target", target)
    assert plan["actions"], "the plan should exercise missions against the target"
    missing_db = kh_root / "runtime" / "tmp" / "synthetic" / "absent.db"

    rc = cli.main(["khunkhaar", "qa", "--plan", plan["plan_id"], "--approve", "--no-verify",
                   "--db", str(missing_db), "--max-steps", "40", "--json"])
    payload = _json(capsys)
    summary = payload["summary"]

    assert rc == 0
    run = store.get_run(payload["run_id"])
    assert run is not None and run["status"] == "completed"
    assert payload["plan_status"] == "completed"
    assert summary["target"]["database"] is False
    assert summary["verification"] == []
    assert summary["findings"]["created"] == 0
    infrastructure = sum(int(agent.get("infrastructure_errors") or 0)
                         for agent in summary["agents"].values())
    assert infrastructure > 0, "a refused connection must be counted, not silently dropped"

    rows = _findings(project_id)
    assert rows == [], "an unreachable target produces infrastructure errors, never findings"
    stats = findings_store.findings_stats(project_id)
    assert stats["by_status"].get("confirmed", 0) == 0

    log_path = kh_root / summary["evidence"]["log"]
    log_text = log_path.read_text(encoding="utf-8")
    assert "db_unavailable" in log_text
    assert "mission_infrastructure_error" in log_text
