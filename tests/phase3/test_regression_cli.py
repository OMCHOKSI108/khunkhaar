"""`khunkhaar regression` as an operator command: refusals, planning, authorization and exit codes.

The tests replace only the execution boundary. Generation, selection, target policy and the exit
classification are the shipped code paths.
"""

from __future__ import annotations

import json

from core import cli
from core.commands import regression as regression_command
from core.metadata import store

import p4_helpers


def test_generate_and_list_report_eligibility_and_the_registry(kh_root, impact_repo, capsys):
    pid = p4_helpers.register(impact_repo.root)
    confirmed = p4_helpers.add_finding(pid, title="Invalid values are accepted")
    likely = p4_helpers.add_finding(pid, title="Maybe a defect", status="likely")

    assert cli.main(["khunkhaar", "regression", "generate", "--path", str(impact_repo.root),
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["finding_id"] for item in payload["generated"]] == [confirmed["id"]]
    assert any(item["finding_id"] == likely["id"] for item in payload["excluded"])
    assert payload["generated"][0]["reliability"] == "generated"

    assert cli.main(["khunkhaar", "regression", "list", "--path", str(impact_repo.root),
                     "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["counts"]["tests"] == 1
    assert listed["tests"][0]["finding_id"] == confirmed["id"]
    assert listed["tests"][0]["latest_run"] is None

    assert cli.main(["khunkhaar", "regression", "list", "--path", str(impact_repo.root),
                     "--finding", likely["id"], "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["tests"] == []


def test_commands_refuse_an_unregistered_repository(kh_root, non_repo, capsys):
    for command in (["regression", "list"], ["regression", "generate"]):
        assert cli.main(["khunkhaar", *command, "--path", str(non_repo)]) == 2
        assert "not a registered project" in capsys.readouterr().out


def test_validate_requires_authorization_for_a_non_loopback_target(
        kh_root, impact_repo, patch_config, monkeypatch, capsys):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Validation admission")
    assert cli.main(["khunkhaar", "regression", "generate", "--path", str(impact_repo.root),
                     "--json"]) == 0
    generated = json.loads(capsys.readouterr().out)["generated"][0]
    test_id = generated["test_id"]
    patch_config("api", {"allowed_hosts": ["10.0.0.1:8740"], "allow_private_ranges": True})

    def explode(*args, **kwargs):
        raise AssertionError("validation must not run without a grant")

    monkeypatch.setattr(regression_command.validate, "validate", explode)
    target = "http://10.0.0.1:8740"
    assert cli.main(["khunkhaar", "regression", "validate", "--path", str(impact_repo.root),
                     "--target", target, "--test", test_id]) == 2
    out = capsys.readouterr().out
    assert "no unexpired authorization grant" in out

    store.grant_authorization(pid, target, "controlled", granted_by="test")
    calls = []

    def fake_validate(project_id, tests, **kwargs):
        calls.append((project_id, [test["test_id"] for test in tests], kwargs))
        return [{"test_id": test_id, "verdict": "trusted", "note": "fixture promoted"}]

    monkeypatch.setattr(regression_command.validate, "validate", fake_validate)
    assert cli.main(["khunkhaar", "regression", "validate", "--path", str(impact_repo.root),
                     "--target", target, "--test", test_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"trusted": 1}
    assert calls and calls[0][0] == pid and calls[0][1] == [test_id]
    assert finding["id"] == generated["finding_id"]

    monkeypatch.setattr(
        regression_command.validate, "validate",
        lambda *args, **kwargs: [{"test_id": test_id, "verdict": "not_reproduced",
                                  "note": "fixture"}])
    assert cli.main(["khunkhaar", "regression", "validate", "--path", str(impact_repo.root),
                     "--target", target, "--test", test_id]) == 1


def test_run_show_plan_never_executes_and_impact_selection_reads_the_saved_analysis(
        kh_root, impact_repo, monkeypatch, capsys):
    pid = p4_helpers.register(impact_repo.root)
    p4_helpers.add_finding(pid, title="Service rejects an empty name",
                           source_refs=("app/service.py:24",))
    assert cli.main(["khunkhaar", "regression", "generate", "--path", str(impact_repo.root),
                     "--json"]) == 0
    test_id = json.loads(capsys.readouterr().out)["generated"][0]["test_id"]

    def explode(*args, **kwargs):
        raise AssertionError("--show-plan must not execute anything")

    monkeypatch.setattr(regression_command.runner, "execute", explode)
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(impact_repo.root),
                     "--base", impact_repo.base_sha, "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["khunkhaar", "regression", "run", "--path", str(impact_repo.root),
                     "--selector", "impact", "--target", "http://127.0.0.1:8740",
                     "--show-plan", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is False
    assert "nothing was executed" in payload["note"]
    plan = payload["plan"]
    assert "app/service.py" in plan["changeset"]["changed_paths"]
    assert [item["test"]["test_id"] for item in plan["selection"]] == [test_id]
    assert "references changed path(s): app/service.py" in plan["selection"][0]["reason"]


def test_run_without_a_saved_analysis_refuses_impact_selection(
        kh_root, impact_repo, capsys):
    pid = p4_helpers.register(impact_repo.root)
    p4_helpers.add_finding(pid, title="No analysis yet", source_refs=("app/service.py:1",))
    assert cli.main(["khunkhaar", "regression", "generate", "--path", str(impact_repo.root),
                     "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["khunkhaar", "regression", "run", "--path", str(impact_repo.root),
                     "--selector", "impact", "--target", "http://127.0.0.1:8740",
                     "--show-plan"]) == 2
    assert "no saved git analysis" in capsys.readouterr().out


def test_run_classifies_pass_and_fail_without_calling_a_real_target(
        kh_root, impact_repo, monkeypatch, capsys):
    pid = p4_helpers.register(impact_repo.root)
    p4_helpers.add_finding(pid, title="Exit code fixture")
    assert cli.main(["khunkhaar", "regression", "generate", "--path", str(impact_repo.root),
                     "--json"]) == 0
    test_id = json.loads(capsys.readouterr().out)["generated"][0]["test_id"]

    def fake_execute(project_id, selection, **kwargs):
        return {"run_id": "fixture-run", "results": [
                    {"test_id": test_id, "result": "pass", "note": "not observed"}],
                "summary": {"by_result": {"pass": 1}, "tests": 1}, "analysis": {}}

    monkeypatch.setattr(regression_command.runner, "execute", fake_execute)
    assert cli.main(["khunkhaar", "regression", "run", "--path", str(impact_repo.root),
                     "--target", "http://127.0.0.1:8740", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is True
    assert payload["execution"]["summary"]["by_result"] == {"pass": 1}

    monkeypatch.setattr(
        regression_command.runner, "execute",
        lambda *args, **kwargs: {
            "run_id": "fixture-fail",
            "results": [{"test_id": test_id, "result": "fail", "note": "still reproduces"}],
            "summary": {"by_result": {"fail": 1}, "tests": 1}, "analysis": {}})
    assert cli.main(["khunkhaar", "regression", "run", "--path", str(impact_repo.root),
                     "--target", "http://127.0.0.1:8740"]) == 1
    out = capsys.readouterr().out
    assert "fail" in out and "still reproduces" in out
