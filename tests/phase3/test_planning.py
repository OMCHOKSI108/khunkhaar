"""Module F: plans are proposals over real inputs, protected by a digest and an approval gate.

Planning never starts a target. The execution tests replace only the mission boundary, so the
store writes, budget, cancellation handling and evidence files are the production ones.
"""

from __future__ import annotations

import asyncio
import json
import stat
import threading
import time

import pytest

from core import cli
from core.findings import store as findings_store
from core.metadata import store
from core.missions.models import Mission
from core.orchestrator import engine
from core.planning import planner
from core.project_discovery import detector
from core.regression import generate
from core.runtime.budget import BudgetExceeded

import p4_helpers

TARGET = "http://127.0.0.1:8740"


def _analyze(repo) -> str:
    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo.root)]) == 0
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(repo.root),
                     "--base", repo.base_sha, "--head", "HEAD"]) == 0
    return detector.project_id_for(repo.root)


def _confirmed_finding(project_id: str) -> dict:
    finding = p4_helpers.add_finding(project_id, title="Invalid values are accepted",
                                     source_refs=("app/service.py:12",))
    generate.generate(finding, project_id=project_id)
    return finding


def _mission(project_id: str, index: int = 1, *, parallel_safe: bool = False) -> Mission:
    return Mission(agent="security_hunter", title=f"read-only probe {index}",
                   kind="standalone:auth_boundary", project_id=project_id,
                   mode_required="controlled", parallel_safe=parallel_safe)


def test_plan_merges_impact_knowledge_and_regression_without_executing(kh_root, impact_repo):
    project_id = _analyze(impact_repo)
    _confirmed_finding(project_id)
    before = impact_repo.state()

    document = planner.build(project_id, impact_repo.root, target=TARGET)

    assert impact_repo.state() == before, "planning never touches the repository"
    assert document["schema"] == planner.SCHEMA
    assert document["llm_usage"] == "none"
    assert document["executable"] is True
    assert document["target"] == TARGET
    assert len(document["digest"]) == 64
    inputs = document["inputs"]
    assert "app/service.py" in inputs["changeset"]["changed_paths"]
    assert inputs["impact"]["summary"]["seeds"] > 0
    assert inputs["impact"]["risk"]["wording"].startswith("This score is an approximate")
    assert inputs["knowledge"]["items"], "the linked finding is retrieved as history"
    assert inputs["regression"]["selected"], "impact selection picked the generated test"
    assert not document["summary"]["requires_approval"], "a loopback controlled plan is unapproved"

    regression = next(a for a in document["actions"] if a["kind"] == "regression")
    assert {t["test_id"] for t in regression["selected_tests"]} == {
        row["test_id"] for row in store.list_regression_tests(project_id)}
    missions = [a for a in document["actions"] if a["kind"] == "mission"]
    assert missions, "the mission generator always proposes its baseline checks"
    for action in missions:
        assert action["agent"] and action["target"] == TARGET
        assert {"steps", "requests", "sessions"} <= set(action["cost"])
        assert action["rationale"]["selection"]
        assert action["mission"]["mode_required"] in ("observe", "controlled", "adversarial")
        assert action["requires_approval"] is False

    serialized = json.dumps(document)
    assert "smoke-value-not-a-real-secret" not in serialized

    row = planner.save(document)
    artifact = kh_root / row["artifact"]
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert row["digest"] == document["digest"]
    assert len(row["actions"]) == len(document["actions"])
    loaded, _ = planner.load(document["plan_id"])
    assert loaded["digest"] == document["digest"]


def test_digest_covers_meaning_and_ignores_bookkeeping(kh_root, impact_repo):
    project_id = _analyze(impact_repo)
    document = planner.build(project_id, impact_repo.root, target=TARGET)
    digest = document["digest"]

    bookkeeping = json.loads(json.dumps(document))
    bookkeeping.update({"status": "approved", "plan_id": "PL-other", "created_at": "later",
                        "artifact": "somewhere", "saved": True,
                        "approval": {"required": False, "approved_at": "now",
                                     "approved_by": "operator"}})
    assert planner.digest_of(bookkeeping) == digest

    meaning = json.loads(json.dumps(document))
    meaning["actions"][0]["cost"]["steps"] += 1
    assert planner.digest_of(meaning) != digest


def test_an_edited_plan_is_refused_as_drifted(kh_root, impact_repo):
    project_id = _analyze(impact_repo)
    document = planner.build(project_id, impact_repo.root, target=TARGET)
    planner.save(document)
    artifact = kh_root / document["artifact"]

    edited = json.loads(artifact.read_text(encoding="utf-8"))
    edited["actions"][0]["target"] = "http://127.0.0.1:9999"
    artifact.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(planner.PlannerError, match="plan drifted"):
        planner.load(document["plan_id"])

    # Even a forged artifact digest cannot match the digest recorded at review time.
    edited["digest"] = planner.digest_of(edited)
    artifact.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(planner.PlannerError, match="plan drifted"):
        planner.load(document["plan_id"])


def test_impact_only_keeps_only_reached_missions_and_records_the_rest(kh_root, impact_repo):
    project_id = _analyze(impact_repo)
    _confirmed_finding(project_id)

    document = planner.build(project_id, impact_repo.root, target=TARGET, impact_only=True)

    mission_actions = [a for a in document["actions"] if a["kind"] == "mission"]
    assert mission_actions, "the linked finding's regression mission is impact-derived"
    for action in mission_actions:
        assert action["rationale"]["impact"], "every kept mission names what reached it"
        assert action["rationale"]["selection"] == "impact-derived"
    excluded = document["summary"]["excluded_missions"]
    assert excluded, "baseline checks that the change set does not reach are listed"
    assert all(item["reason"] for item in excluded)
    assert {item["mission_id"] for item in excluded}.isdisjoint(
        {action["mission"]["mission_id"] for action in mission_actions})


def test_an_impact_only_plan_without_a_change_set_is_honestly_empty(kh_root, impact_repo):
    project_id = p4_helpers.register(impact_repo.root)

    document = planner.build(project_id, impact_repo.root, target=TARGET, impact_only=True)

    assert not [a for a in document["actions"] if a["kind"] == "mission"]
    notes = " ".join(document["inputs"]["notes"])
    assert "impact-only plan requires a saved change set" in notes
    assert "empty plan is reported" in notes
    assert document["inputs"]["regression"]["selected"] == []
    assert "needs a saved change set" in document["inputs"]["regression"]["note"]


def test_adversarial_and_external_actions_require_approval_with_a_timestamp(kh_root, impact_repo):
    project_id = _analyze(impact_repo)

    adversarial = planner.build(project_id, impact_repo.root, target=TARGET, mode="adversarial")
    adversarial_actions = [a for a in adversarial["actions"] if a["mode"] == "adversarial"]
    assert adversarial_actions, "adversarial plans include journey mutations"
    assert all(a["requires_approval"] for a in adversarial_actions)
    assert adversarial["summary"]["requires_approval"] is True
    assert adversarial["approval"]["required"] is True

    external = planner.build(project_id, impact_repo.root,
                             target="http://10.20.30.40:8080")
    assert external["actions"]
    assert all(a["requires_approval"] for a in external["actions"])
    assert set(external["summary"]["approval_actions"]) == {
        action["action_id"] for action in external["actions"]}
    assert external["approval"] == {"required": True, "approved_at": None, "approved_by": None}

    row = planner.save(external)
    digest = row["digest"]
    approved = planner.approve(row["plan_id"], approved_by="mira")
    assert approved["status"] == "approved"
    assert approved["approval"]["approved_by"] == "mira"
    assert approved["approval"]["approved_at"]
    assert approved["digest"] == digest
    stored = store.get_qa_plan(row["plan_id"])
    assert stored["status"] == "approved" and stored["approved_at"] == (
        approved["approval"]["approved_at"])

    planner.mark(row["plan_id"], "executing")
    with pytest.raises(planner.PlannerError, match="already executing"):
        planner.load_for_execution(row["plan_id"])
    planner.mark(row["plan_id"], "completed")
    with pytest.raises(planner.PlannerError, match="already ran"):
        planner.load_for_execution(row["plan_id"])


def test_a_plan_without_a_target_is_not_executable(kh_root, impact_repo):
    project_id = _analyze(impact_repo)
    document = planner.build(project_id, impact_repo.root)
    assert document["executable"] is False
    assert "no target" in document["executable_reason"]
    planner.save(document)
    with pytest.raises(planner.PlannerError, match="no target"):
        planner.load_for_execution(document["plan_id"])


def test_execution_reconstructs_exactly_the_planned_missions(kh_root, impact_repo, monkeypatch):
    project_id = _analyze(impact_repo)
    document = planner.build(project_id, impact_repo.root, target=TARGET)
    missions = planner.plan_missions(document)
    assert missions

    orchestrator = engine.Orchestrator(engine.Options(
        root=impact_repo.root, command="qa", plan_id=document["plan_id"],
        planned_missions=missions, run_id="run-equivalence"))
    seen: list[str] = []

    async def fake(mission):
        seen.append(mission.mission_id)
        return engine.AgentOutcome(mission.agent, mission)

    monkeypatch.setattr(orchestrator, "_execute_mission", fake)
    summary = asyncio.run(orchestrator.run())

    assert seen == [mission["mission_id"] for mission in missions]
    assert summary["missions"]["planned"] == len(missions)
    assert summary["missions"]["executed"] == len(missions)
    assert summary["plan_id"] == document["plan_id"]
    assert summary["llm_usage"] == "none"


def test_an_empty_planned_set_is_not_replaced_by_generated_missions(
        kh_root, impact_repo, monkeypatch):
    project_id = p4_helpers.register(impact_repo.root)
    orchestrator = engine.Orchestrator(engine.Options(
        root=impact_repo.root, command="qa", planned_missions=[], run_id="run-empty-plan"))
    called = []

    async def fake(mission):  # pragma: no cover - the assertion is that it never runs
        called.append(mission)
        return engine.AgentOutcome(mission.agent, mission)

    monkeypatch.setattr(orchestrator, "_execute_mission", fake)
    summary = asyncio.run(orchestrator.run())

    assert called == []
    assert summary["missions"]["planned"] == 0
    assert findings_store.findings_stats(project_id)["total"] == 0


def test_cancellation_mid_run_records_cancelled_with_partial_evidence(
        kh_root, impact_repo, monkeypatch):
    project_id = p4_helpers.register(impact_repo.root)
    missions = [_mission(project_id, 1), _mission(project_id, 2)]
    orchestrator = engine.Orchestrator(engine.Options(
        root=impact_repo.root, command="qa", planned_missions=[m.to_dict() for m in missions],
        run_id="run-cancel"))

    async def fake(mission):
        orchestrator.budget.cancel()
        return engine.AgentOutcome(mission.agent, mission)

    monkeypatch.setattr(orchestrator, "_execute_mission", fake)
    summary = asyncio.run(orchestrator.run())

    run = store.get_run("run-cancel")
    assert run["status"] == "cancelled"
    assert run["llm_usage"] == "none"
    assert summary["resources"]["llm_usage"] == "none"
    assert summary["missions"]["executed"] == 1
    assert summary["missions"]["skipped"] == 1
    assert any("cancelled" in detail
               for detail in summary["missions"]["skipped_detail"])
    assert (kh_root / summary["evidence"]["run"]).is_file()
    assert summary["findings"]["created"] == 0


def test_budget_exhaustion_keeps_partial_evidence_and_never_claims_a_defect(
        kh_root, impact_repo, monkeypatch):
    project_id = p4_helpers.register(impact_repo.root)
    orchestrator = engine.Orchestrator(engine.Options(
        root=impact_repo.root, command="qa",
        planned_missions=[_mission(project_id, 1).to_dict()],
        run_id="run-budget", max_steps=1, max_requests=1))

    async def fake(mission):
        orchestrator.budget.consume_steps(2)

    monkeypatch.setattr(orchestrator, "_execute_mission", fake)
    with pytest.raises(BudgetExceeded):
        asyncio.run(orchestrator.run())

    run = store.get_run("run-budget")
    assert run["status"] == "budget_exceeded"
    assert orchestrator.summary["errors"]
    assert "findings" not in orchestrator.summary
    assert (kh_root / orchestrator.summary["evidence"]["run"]).is_file()
    assert findings_store.findings_stats(project_id)["total"] == 0


def test_read_only_missions_use_a_bounded_pool_and_keep_plan_order(
        kh_root, impact_repo, patch_config, monkeypatch):
    patch_config("planner", {"api_workers": 2})
    orchestrator = engine.Orchestrator(engine.Options(root=impact_repo.root, command="qa"))
    missions = [_mission("p", 1, parallel_safe=True), _mission("p", 2, parallel_safe=True)]
    threads: list[str] = []

    def fake(mission):
        async def run():
            threads.append(threading.current_thread().name)
            time.sleep(0.05)
            return engine.AgentOutcome(mission.agent, mission)

        return run()

    monkeypatch.setattr(orchestrator, "_execute_mission", fake)
    results = orchestrator._execute_parallel(missions)

    assert [outcome.mission.mission_id for outcome in results] == [
        mission.mission_id for mission in missions], "results stay in plan order"
    assert len(set(threads)) == 2
    assert all(name.startswith("khunkhaar-readonly") for name in threads)

    orchestrator.budget.max_concurrency = 1
    threads.clear()
    orchestrator._execute_parallel(missions)
    assert threads == [threading.current_thread().name] * 2, (
        "a concurrency cap of one keeps execution on the calling thread")


def test_batching_stops_at_a_state_changing_mission(
        kh_root, impact_repo, patch_config, monkeypatch):
    patch_config("planner", {"api_workers": 4})
    project_id = p4_helpers.register(impact_repo.root)
    orchestrator = engine.Orchestrator(engine.Options(root=impact_repo.root, command="qa"))
    orchestrator.ops = [{"id": "GET /items"}]
    orchestrator.summary = {"missions": {"planned": 3, "executed": 0, "skipped": 0,
                                         "failed": 0, "skipped_detail": []}}
    read_only = [_mission(project_id, 1, parallel_safe=True),
                 _mission(project_id, 2, parallel_safe=True)]
    write = _mission(project_id, 3, parallel_safe=False)
    batches: list[list[str]] = []
    serial: list[str] = []

    def fake_parallel(batch):
        batches.append([mission.mission_id for mission in batch])
        return [engine.AgentOutcome(mission.agent, mission) for mission in batch]

    async def fake_serial(mission):
        serial.append(mission.mission_id)
        return engine.AgentOutcome(mission.agent, mission)

    monkeypatch.setattr(orchestrator, "_execute_parallel", fake_parallel)
    monkeypatch.setattr(orchestrator, "_execute_mission", fake_serial)
    asyncio.run(orchestrator._execute_missions([*read_only, write]))

    assert batches == [[mission.mission_id for mission in read_only]]
    assert serial == [write.mission_id], "a state-changing mission is never pooled"
    assert orchestrator.summary["missions"]["executed"] == 3
