"""P8: end-to-end acceptance on synthetic repositories.

The Python repository is a copy of the Phase 2 `vulnapp` application, served from its own checkout
on a loopback port with a database inside the isolated KHUNKHAAR tree. Every step below is the
shipped code path: registration, git analysis, impact/risk, historical regression selection, the
planner and its approval gate, real probes with independent verification, test generation and
subprocess validation, reporting, the dashboard and the isolation check.

Execution is deliberately narrowed to the planned missions of the three API-side agents
(`security_hunter`, `api_hunter`, `workflow_breaker`) with the plan's selected regression test
replayed as well. The stored plan is not regenerated for this: the exact mission specifications
are reconstructed from the saved document. Actions that propose browser work or write-style
database checks are left unexecuted and are covered by their own suites, so the acceptance run is
bounded and deterministic instead of depending on a browser toolchain.

The TypeScript repository is analysed by git/impact only. No TypeScript runtime is bundled, so the
suite asserts what the analysers report and never claims to execute the application.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import acceptance_fixtures as fx
import p4_helpers
from core import cli
from core.findings import store as findings_store
from core.metadata import store
from core.orchestrator import engine
from core.planning import planner
from core.project_discovery import detector
from core.regression import generate, select, validate
from dashboard.app import create_app

DASHBOARD_TOKEN = "acceptance-dashboard-token-" + "y" * 24
EXECUTED_AGENTS = ("security_hunter", "api_hunter", "workflow_breaker")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _live_application(repo: fx.AcceptanceRepo, kh_root: Path, monkeypatch):
    """Serve the repository's own app.py with its database inside the KHUNKHAAR tree.

    The module is imported under a unique name from the checkout, so a repository edit (not a test
    fixture) would change what the probes see. `VULN_DB` must be set before the import because the
    app resolves its database path at module load.
    """
    db_path = kh_root / "runtime" / "tmp" / "synthetic" / "vulnapp.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VULN_DB", str(db_path))
    # Importing from the checkout must not leave a __pycache__ behind: the isolation check
    # asserts the repository is byte-identical after the run.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module_name = f"acceptance_vulnapp_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, repo.root / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="acceptance-vulnapp")
    thread.start()
    target = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{target}/openapi.json", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        sys.modules.pop(module_name, None)
        pytest.fail("the acceptance application did not start within 20s")
    try:
        yield target, db_path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sys.modules.pop(module_name, None)


def _counter(project_id: str, table: str) -> int:
    with store.connect() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE project_id=?",
                            (project_id,)).fetchone()["n"]


def test_python_repository_full_acceptance_chain(
        kh_root, tmp_path, capsys, patch_config, monkeypatch):
    repo = fx.build_py_project(tmp_path)
    state_before = repo.state()

    # 1. Register the repository: registration is bookkeeping under the KHUNKHAAR tree only.
    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo.root)]) == 0
    project_id = detector.project_id_for(repo.root)
    assert project_id
    assert Path(store.get_project(project_id)["root_path"]).resolve() == repo.root.resolve()
    capsys.readouterr()

    # 2. Analyse exactly one feature commit against its base.
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(repo.root),
                     "--base", repo.base_sha, "--head", "HEAD"]) == 0
    capsys.readouterr()
    analysis = store.latest_git_analysis(project_id)
    assert analysis["kind"] == "range"
    changeset = json.loads((kh_root / analysis["artifact"]).read_text(encoding="utf-8"))["changeset"]
    files = {item["path"]: item for item in changeset["files"]}
    assert set(files) == {"app.py", "schema.sql", "docs/WORKFLOW.md"}
    assert {"create_request", "get_request", "approve_request"} <= set(
        files["app.py"].get("functions") or [])
    assert changeset["summary"]["index_untouched"] is True
    assert changeset["summary"]["worktree_clean"] is True

    # 3. Impact and risk: the change reaches the API surface, entities and the existing test,
    #    and the score is presented as attention, never as a probability.
    assert cli.main(["khunkhaar", "impact", "--path", str(repo.root)]) == 0
    capsys.readouterr()
    impact_row = store.latest_impact_report(project_id)
    impact_doc = json.loads((kh_root / impact_row["artifact"]).read_text(encoding="utf-8"))
    surface = {(item["type"], item["name"]) for item in impact_doc["impact"]["surface"]}
    assert ("api", "POST /api/requests") in surface
    assert ("api", "GET /api/requests/{}") in surface
    assert ("entity", "requests") in surface
    assert [item["path"] for item in impact_doc["impact"]["tests"]] == ["tests/test_app.py"]
    assert "requests" in [item["name"] for item in impact_doc["impact"]["workflows"]]
    assert round(sum(item["contribution"] for item in impact_doc["risk"]["factors"]), 2) == (
        impact_doc["risk"]["score"])
    assert "not a proven probability" in impact_doc["risk"]["wording"]

    with _live_application(repo, kh_root, monkeypatch) as (target, db_path):
        patch_config("api", {"allowed_hosts": [
            "127.0.0.1:8731", "localhost:8731", target.rsplit("/", 1)[-1],
            target.rsplit("/", 1)[-1].replace("127.0.0.1", "localhost"),
        ]})

        # 4. Historical knowledge: a previously confirmed defect becomes a regression test that
        #    the plan will select because its source reference is in the change set.
        prior = p4_helpers.add_finding(
            project_id,
            title="A request amount must be positive and its title non-empty",
            feature="requests", category="api", rule_ids=("R4",),
            source_refs=("app.py:145",),
            reproduction=p4_helpers.probe_reproduction(
                "validation", rule_id="R4", feature="requests",
                description="A request amount must be positive and its title non-empty"))
        prior_test = generate.generate(prior, project_id=project_id)
        assert prior_test["test"]["reliability"] == "generated"
        assert (kh_root / prior_test["path"]).is_file()

        # 5. Build an impact-restricted plan through the shipped command; every mission carries
        #    the reason it was selected, and unmatched baseline missions are excluded by name.
        assert cli.main(["khunkhaar", "plan", "--path", str(repo.root), "--target", target,
                         "--impact-only", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        document, stored_plan = planner.load(payload["plan_id"])
        assert document["executable"] is True and document["impact_only"] is True
        assert stored_plan["status"] == "draft"
        assert any("not a probability" in line for line in document["wording"])
        assert any("a plan is a proposal" in line for line in document["wording"])
        assert document["summary"]["excluded_missions"], "unmatched missions are reported"
        assert all(item["reason"] for item in document["summary"]["excluded_missions"])

        mission_actions = [action for action in document["actions"] if action["kind"] == "mission"]
        regression_action = next(action for action in document["actions"]
                                 if action["kind"] == "regression")
        assert [item["test_id"] for item in regression_action["selected_tests"]] == [
            prior_test["test"]["test_id"]]
        assert "app.py" in regression_action["selected_tests"][0]["reason"]
        executed = [action for action in mission_actions if action["agent"] in EXECUTED_AGENTS]
        assert {(action["agent"], action["mission"]["payload"].get("probe_kind"))
                for action in executed} == {
            ("security_hunter", "authorization"), ("api_hunter", "validation"),
            ("workflow_breaker", "workflow"), ("workflow_breaker", "idempotency")}
        for action in executed:
            assert action["rationale"]["selection"] == "impact-derived"
            assert action["rationale"]["impact"], "each action says what matched the change set"

        # 6. The plan needs approval before execution; the refusal is recorded and no run exists.
        assert document["summary"]["requires_approval"] is True
        assert cli.main(["khunkhaar", "qa", "--plan", payload["plan_id"]]) == 2
        assert "require" in capsys.readouterr().out
        assert _counter(project_id, "qa_runs") == 0
        assert cli.main(["khunkhaar", "plan", "approve", "--plan", payload["plan_id"],
                         "--by", "acceptance"]) == 0
        capsys.readouterr()
        approved = store.get_qa_plan(payload["plan_id"])
        assert approved["status"] == "approved" and approved["approved_at"]
        assert approved["approved_by"] == "acceptance"

        # 7. Execute exactly the stored mission specifications plus the planned regression replay.
        run_id = f"acceptance-{uuid.uuid4().hex[:10]}"
        planner.mark(payload["plan_id"], "executing", run_id=run_id)
        orchestrator = engine.Orchestrator(engine.Options(
            root=repo.root, target=target, db_path=str(db_path),
            users_file=str(repo.root / "qa-users.json"), mode="controlled", verify=True,
            command="qa", plan_id=payload["plan_id"],
            planned_missions=[action["mission"] for action in executed],
            planned_regression=planner.plan_regression(document), run_id=run_id))
        summary = asyncio.run(orchestrator.run())
        planner.mark(payload["plan_id"], "completed", run_id=run_id)

        run = store.get_run(run_id)
        assert run["status"] == "completed" and run["llm_usage"] == "none"
        assert store.get_qa_plan(payload["plan_id"])["status"] == "completed"
        assert summary["plan_id"] == payload["plan_id"]
        assert summary["target"]["connected"] is True
        assert summary["target"]["contract"] == "openapi"
        assert summary["llm_usage"] == "none"
        assert (kh_root / summary["evidence"]["run"]).is_file()
        assert (kh_root / summary["evidence"]["log"]).is_file()

        # The historical test reproduced again: "fail" is the saved reproduction still holding,
        # not a regression in the application.
        regression = summary["regression"]["execution"]
        assert [item["test_id"] for item in regression["results"]] == [
            prior_test["test"]["test_id"]]
        assert regression["results"][0]["result"] == "fail"
        assert "saved reproduction still reproduces" in regression["results"][0]["note"]

        # The adversarial-only idempotency mission is skipped with a visible reason, never run
        # outside its allowed mode and never counted as a finding.
        assert summary["missions"] == {
            **summary["missions"], "planned": 4, "executed": 3, "skipped": 1, "failed": 0}
        assert any("adversarial" in detail for detail in summary["missions"]["skipped_detail"])

        # 8. Findings: three defects were observed, then independently verified twice each.
        assert summary["findings"]["created"] == 3
        assert summary["findings"]["updated"] == 0
        assert summary["findings"]["total"] == 4
        assert len(summary["verification"]) == 3
        for item in summary["verification"]:
            assert item["status"] == "confirmed"
            assert item["attempts"] == 2 and item["reproductions"] == 2
        stats = findings_store.findings_stats(project_id)
        assert stats["total"] == 4 and stats["by_status"] == {"confirmed": 4}
        rows = findings_store.list_findings(project_id)
        assert {row["status"] for row in rows} == {"confirmed"}
        rules = {rule for row in rows for rule in row.get("rule_ids") or []}
        executed_rules = {
            (action["mission"]["payload"].get("invariant") or {}).get("rule_id")
            for action in executed
            if action["mission"]["payload"].get("probe_kind") != "idempotency"}
        assert executed_rules <= rules
        assert "R4" in rules, "the historical finding's rule is preserved"
        assert {"security", "api", "business_logic"} <= {row["category"] for row in rows}
        fresh = [row for row in rows if row["id"] != prior["id"]]
        assert len(fresh) == 3
        for row in fresh:
            assert row["evidence"] and row["steps"] and row["expected"] and row["actual"]
            assert row["remediation"]

        # 9. Only confirmed findings are eligible; generation is deterministic and redacted.
        selection = select.select(project_id)
        assert len(selection["eligible"]) == 4 and selection["excluded"] == []
        generated = generate.generate_for_findings(project_id, selection["eligible"])
        assert len(generated) == 4
        assert all((kh_root / item["path"]).is_file() for item in generated)

        # 10. Each generated test reproduces the defect in both normal runs; the inverted
        #     observation passes, which is what promotes it to trusted.
        reports = validate.validate(
            project_id, [item["test"] for item in generated], target=target,
            users_path=str(repo.root / "qa-users.json"), db_path=str(db_path), runs=2)
        assert {item["verdict"] for item in reports} == {"trusted"}
        for item in reports:
            artifact = json.loads((kh_root / item["artifact"]).read_text(encoding="utf-8"))
            assert len(artifact["normal_runs"]) == 2
            assert all(run["outcome"]["violated"] is True for run in artifact["normal_runs"])
            assert artifact["inverted_run"]["rc"] == 0
            assert artifact["inverted_run"]["outcome"]["violated"] is True
            tested = store.get_regression_test(item["test_id"])
            assert tested["reliability"] == "trusted" and tested["validated_path"]

        # 11. The report is generated from the store, split into confirmed and unverified parts.
        assert cli.main(["khunkhaar", "report", "--project-root", str(repo.root), "--json"]) == 0
        written = json.loads(capsys.readouterr().out)
        report_json, report_md = kh_root / written["json"], kh_root / written["markdown"]
        assert report_json.is_file() and report_md.is_file()
        assert report_json.stat().st_mode & 0o777 == 0o600
        assert report_md.stat().st_mode & 0o777 == 0o600
        report = json.loads(report_json.read_text(encoding="utf-8"))
        assert report["stats"] == {**report["stats"], "total": 4,
                                   "by_status": {"confirmed": 4}}
        assert len(report["findings"]) == 4
        assert len(report["invariants"]) >= 3
        markdown = report_md.read_text(encoding="utf-8")
        assert "## Confirmed findings (independently reproduced)" in markdown
        assert "## Unverified, not reproducible or needing human judgment" in markdown

        # 12. The dashboard renders the same store read-only, behind the token gate.
        application = create_app(token=DASHBOARD_TOKEN)
        browser = TestClient(application)
        assert browser.post("/login", data={"token": DASHBOARD_TOKEN},
                            follow_redirects=False).status_code == 303
        overview = browser.get("/")
        assert overview.status_code == 200 and project_id in overview.text
        assert browser.get(f"/project/{project_id}").status_code == 200
        execution_page = browser.get(f"/execution/{run_id}")
        assert execution_page.status_code == 200
        assert "Verification" in execution_page.text and "confirmed" in execution_page.text
        assert "Browser, API and database evidence" in execution_page.text
        diff_page = browser.get(f"/diff/{project_id}")
        assert diff_page.status_code == 200 and "app.py" in diff_page.text
        refs = store.list_evidence_refs(project_id=project_id)
        assert refs
        servable = next(ref for ref in refs
                        if str(ref["path"]).startswith("evidence/")
                        and Path(str(ref["path"])).suffix.lower() in (".json", ".txt", ".md", ".log"))
        evidence = browser.get(f"/evidence/{servable['id']}")
        assert evidence.status_code == 200
        assert evidence.headers["content-type"].startswith("text/plain")
        for ref in refs:
            if not str(ref["path"]).startswith(("evidence/", "reports/")):
                assert browser.get(f"/evidence/{ref['id']}").status_code == 404, (
                    "a reference outside the evidence/reports roots is never served")
        assert findings_store.findings_stats(project_id)["total"] == 4

    # 13. Isolation: every run artifact lives under the KHUNKHAAR root and the repository is
    #     byte-identical, including its index.
    for relative in (analysis["artifact"], impact_row["artifact"], document["artifact"],
                     written["json"], written["markdown"], summary["evidence"]["run"],
                     summary["evidence"]["log"]):
        assert (kh_root / relative).resolve().is_relative_to(kh_root.resolve())
    assert repo.state() == state_before, "the analysed repository was not modified"


def test_typescript_repository_is_analysed_but_never_executed(kh_root, tmp_path, capsys):
    repo = fx.build_ts_project(tmp_path)
    state_before = repo.state()

    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo.root)]) == 0
    project_id = detector.project_id_for(repo.root)
    assert project_id
    capsys.readouterr()

    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(repo.root),
                     "--base", repo.base_sha, "--head", "HEAD"]) == 0
    capsys.readouterr()
    analysis = store.latest_git_analysis(project_id)
    changeset = json.loads((kh_root / analysis["artifact"]).read_text(encoding="utf-8"))["changeset"]
    files = {item["path"]: item for item in changeset["files"]}
    assert set(files) == {"src/users/users.controller.ts", "src/users/users.service.ts",
                          "prisma/schema.prisma", "docs/WORKFLOW.md"}
    assert changeset["summary"]["files_changed"] == 4
    assert changeset["summary"]["index_untouched"] is True
    assert changeset["summary"]["worktree_clean"] is True
    # A heuristic attribution is labelled as one; an unparsed file is 'none', never 'no symbols'.
    assert files["src/users/users.controller.ts"]["attribution"] == "git-hunk-header"
    assert files["prisma/schema.prisma"]["attribution"] == "none"

    assert cli.main(["khunkhaar", "impact", "--path", str(repo.root)]) == 0
    capsys.readouterr()
    impact_row = store.latest_impact_report(project_id)
    impact_doc = json.loads((kh_root / impact_row["artifact"]).read_text(encoding="utf-8"))
    surface = {(item["type"], item["name"]) for item in impact_doc["impact"]["surface"]}
    assert ("api", "GET /users/{}") in surface and ("api", "POST /users") in surface
    assert ("entity", "Team") in surface and ("entity", "User") in surface
    assert [item["path"] for item in impact_doc["impact"]["tests"]] == [
        "test/users.controller.spec.ts"]
    impacted = {(item["type"], item["name"]) for item in impact_doc["impact"]["impacted"]}
    assert ("test", "test/users.controller.spec.ts") in impacted
    # The workflow document changed but no Markdown parser exists here; the gap is recorded
    # instead of being silently treated as "no impact".
    assert impact_doc["impact"]["missing"][0]["path"] == "docs/WORKFLOW.md"
    assert "not in the scanned graph" in impact_doc["impact"]["missing"][0]["reason"]
    assert 0 <= impact_doc["risk"]["score"] <= 100
    assert impact_doc["risk"]["attention_band"] == "low attention"
    assert "not a proven probability" in impact_doc["risk"]["wording"]
    assert round(sum(item["contribution"] for item in impact_doc["risk"]["factors"]), 2) == (
        impact_doc["risk"]["score"])

    # The six fixture conditions are present in the analysed checkout: validation removed,
    # unscoped read in the controller, a new relation/index, a linked e2e scenario and a
    # modified workflow document.
    controller = (repo.root / "src/users/users.controller.ts").read_text(encoding="utf-8")
    assert "findOneUnscoped" in controller and "findOneForCaller" not in controller
    assert ".includes(\"@\")" not in controller
    schema = (repo.root / "prisma/schema.prisma").read_text(encoding="utf-8")
    assert "teamId Int" in schema and "@@index([teamId])" in schema
    assert (repo.root / "test/users.controller.spec.ts").is_file()
    assert "Any authenticated caller may read any profile" in (
        repo.root / "docs/WORKFLOW.md").read_text(encoding="utf-8")

    # Analysis only: no plan, run, finding or regression test was produced, and the repository
    # index and worktree are untouched.
    assert _counter(project_id, "qa_runs") == 0
    assert _counter(project_id, "qa_plans") == 0
    assert _counter(project_id, "regression_tests") == 0
    findings_store.init()
    assert findings_store.findings_stats(project_id)["total"] == 0
    assert repo.state() == state_before
