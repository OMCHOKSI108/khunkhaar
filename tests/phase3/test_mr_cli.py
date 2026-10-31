"""Module B through the shipped CLI: merge-base local refs, composition, persistence.

Every repository is synthetic. The GitLab network boundary is exercised in
`test_gitlab_adapter.py`; here it is replaced with a stub only where the CLI's handling of
successful metadata is under test.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from core.cli import main as cli
from core.findings import models as finding_models
from core.findings import store as findings_store
from core.metadata import store

TOKEN_ENV = "KHUNKHAAR_TEST_GITLAB_TOKEN"


def register(path) -> int:
    return cli(["khunkhaar", "project", "register", "--path", str(path), "--json"])


def git_analyze(path, *flags: str) -> int:
    return cli(["khunkhaar", "git", "analyze", "--path", str(path), *flags])


def mr_analyze(path, *flags: str) -> int:
    return cli(["khunkhaar", "mr", "analyze", "--path", str(path), *flags])


def _project(root: Path) -> dict:
    return next(item for item in store.list_projects()
                if Path(item["root_path"]).resolve() == root.resolve())


def _count(table: str, project_id: str) -> int:
    with store.connect() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE project_id=?",  # noqa: S608
                            (project_id,)).fetchone()["n"]


def _stable(value):
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()
                if key != "graph_generated_at"}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _local(payload: dict) -> dict:
    return _stable({key: value for key, value in payload.items()
                    if key not in ("gitlab", "graph", "saved", "artifact", "report_id",
                                   "analysis_id")})


def test_git_analyze_mr_aliases_use_the_merge_base(kh_root, mr_repo, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    rc = git_analyze(mr_repo.root, "--mr-source", "feature/items", "--mr-target", "main",
                     "--no-save", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    changeset = payload["changeset"]
    assert changeset["kind"] == "merge-request"
    assert changeset["base"] == "main" and changeset["head"] == "feature/items"
    assert changeset["summary"]["files_changed"] == 1
    assert [item["path"] for item in changeset["files"]] == ["app/service.py"]
    assert any("merge-request comparison" in note for note in changeset["notes"])
    assert payload["requested"]["mr_source"] == "feature/items"
    assert payload["requested"]["mr_target"] == "main"


def test_mr_aliases_refuse_mixed_modes(kh_root, mr_repo, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    assert git_analyze(mr_repo.root, "--mr-source", "feature/items", "--base", "main") == 2
    out = capsys.readouterr().out
    assert "cannot be combined" in out and "--base" in out
    assert git_analyze(mr_repo.root, "--mr-target", "main") == 2
    assert "requires --mr-source" in capsys.readouterr().out


def test_mr_analyze_composes_the_review_locally(kh_root, mr_repo, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    before = mr_repo.state()
    rc = mr_analyze(mr_repo.root, "--base", "main", "--head", "feature/items",
                    "--no-save", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert mr_repo.state() == before, "the repository index and worktree are untouched"

    mr = payload["merge_request"]
    assert mr["kind"] == "merge-request"
    assert mr["target"] == "main" and mr["source"] == "feature/items"
    assert mr["summary"]["files_changed"] == 1

    modules = {item["module"]: item for item in payload["modules"]}
    assert modules["app"]["files"] == 1
    assert modules["app"]["languages"] == ["python"]

    affected = payload["affected"]
    names = {item["name"] for item in affected["api_endpoints"]}
    assert {"GET /items", "POST /items"} <= names
    assert [item["name"] for item in affected["database_models"]] == ["items"]
    assert [item["name"] for item in affected["workflows"]] == ["items"]
    assert [item["path"] for item in affected["tests"]] == ["tests/test_service.py"]

    assert payload["security"]["auth_signals"], "the fixture uses a FastAPI dependency"
    assert payload["security"]["implications"]
    assert payload["regressions"]["coverage"]["source_files_changed"] == 1

    factors = payload["risk"]["factors"]
    assert len(factors) == 6
    assert round(sum(item["contribution"] for item in factors), 2) == payload["risk"]["score"]
    from core.impact import risk as impact_risk
    assert payload["risk"]["wording"] == impact_risk.WORDING

    plan = payload["recommended_qa_plan"]
    assert plan
    assert all(item["status"] == "recommended - not executed" for item in plan)
    assert "not a result" in payload["plan_execution"]
    assert any(item["area"] == "api" for item in plan)
    assert payload["gitlab"]["status"] == "disabled"
    assert payload["saved"] is False and payload["report_id"] is None


def test_mr_analyze_saves_artifact_rows_and_no_secret(kh_root, mr_repo, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    assert mr_analyze(mr_repo.root, "--base", "main", "--head", "feature/items",
                      "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    project_id = _project(mr_repo.root)["project_id"]

    artifact = Path(payload["artifact"])
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
    text = artifact.read_text(encoding="utf-8")
    assert "smoke-value-not-a-real-secret" not in text
    recorded = json.loads(text)
    assert recorded["saved"] is True
    assert recorded["risk"]["score"] == payload["risk"]["score"]
    assert recorded["recommended_qa_plan"] == payload["recommended_qa_plan"]

    row = store.latest_git_analysis(project_id)
    assert row["kind"] == "merge-request"
    assert row["id"] == payload["analysis_id"]
    assert row["artifact"] == payload["artifact"].split(str(kh_root))[-1].lstrip("/")

    report = store.latest_impact_report(project_id)
    assert report["report_id"] == payload["report_id"]
    assert report["analysis_id"] == payload["analysis_id"]
    assert report["risk_score"] == payload["risk"]["score"]
    assert len(report["risk_factors"]) == 6

    # The saved review artifact keeps the change-set contract, so Module C can chain onto it
    # without a second git analysis.
    rc = cli(["khunkhaar", "impact", "--path", str(mr_repo.root), "--no-save", "--json"])
    assert rc == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["changeset"]["kind"] == "merge-request"
    assert reviewed["changeset"]["base"] == "main"
    assert reviewed["changeset"]["head"] == "feature/items"
    assert reviewed["impact"]["summary"]["seeds"] >= 1


def test_missing_remote_tracking_ref_is_an_infrastructure_error(kh_root, mr_repo, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    project_id = _project(mr_repo.root)["project_id"]
    rc = mr_analyze(mr_repo.root, "--base", "origin/main", "--head", "feature/items", "--json")
    assert rc == 1
    out = capsys.readouterr().out
    assert "does not resolve locally" in out
    assert "never fetches" in out
    assert "defect" not in out.lower()
    assert _count("git_analyses", project_id) == 0
    assert _count("impact_reports", project_id) == 0
    assert _count("findings", project_id) == 0


def test_a_confirmed_finding_on_a_changed_path_reaches_security_and_the_plan(kh_root, mr_repo,
                                                                              capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    project_id = _project(mr_repo.root)["project_id"]
    finding = finding_models.Finding(
        project_id=project_id, feature="items", category="security",
        title="Item update ignores the owner check", description="A second user can rename "
        "another user's item.", severity="high", status="confirmed", confidence=0.95,
        source_refs=["app/service.py:12"], steps=["rename an item as another user"],
        expected="403", actual="200")
    stored, created = findings_store.add_finding(finding, actor="pytest")
    assert created

    assert mr_analyze(mr_repo.root, "--base", "main", "--head", "feature/items",
                      "--no-save", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["security"]["findings"]] == [stored["id"]]
    assert [item["id"] for item in payload["regressions"]["historical_findings"]] == [
        stored["id"]]
    actions = [item["action"] for item in payload["recommended_qa_plan"]]
    assert any("verified reproduction" in action and stored["id"] in action
               for action in actions)


def test_gitlab_unavailable_leaves_the_local_review_identical(kh_root, mr_repo, patch_config,
                                                              monkeypatch, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()
    assert mr_analyze(mr_repo.root, "--no-save", "--json") == 0
    baseline = json.loads(capsys.readouterr().out)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()
    patch_config("gitlab", {
        "enabled": True, "host": f"http://127.0.0.1:{closed_port}", "project": "group/proj",
        "token_env": TOKEN_ENV, "timeout_seconds": 1,
    })
    patch_config("api", {"allowed_hosts": [f"127.0.0.1:{closed_port}"]})
    monkeypatch.setenv(TOKEN_ENV, "glpat-never-written")

    assert mr_analyze(mr_repo.root, "--gitlab-mr", "7", "--no-save", "--json") == 0
    offline = json.loads(capsys.readouterr().out)
    assert offline["gitlab"]["status"] == "unavailable"
    assert "unreachable" in offline["gitlab"]["reason"]
    assert _local(offline) == _local(baseline), (
        "a GitLab outage must not change the local review")
    assert "glpat-never-written" not in json.dumps(offline)


def test_gitlab_metadata_defaults_the_refs_and_is_compared_with_the_local_head(
        kh_root, mr_repo, monkeypatch, capsys):
    assert register(mr_repo.root) == 0
    capsys.readouterr()

    def fake_fetch(iid, *, project=None, cfg=None):
        assert str(iid) == "7"
        return {
            "status": "ok", "reason": "fetched", "host": "http://gitlab.invalid",
            "project": "group/proj",
            "merge_request": {
                "iid": 7, "title": "Add the rename method", "state": "opened",
                "source_branch": "feature/items", "target_branch": "main",
                "sha": mr_repo.branches["feature/items"],
                "web_url": "http://gitlab.invalid/group/proj/-/merge_requests/7",
            },
            "diff": {"fetched": True, "file_count": 1,
                     "files": [{"new_path": "app/service.py"}], "truncated": False},
        }

    monkeypatch.setattr("core.commands.mr.gitlab_adapter.fetch_merge_request", fake_fetch)
    assert mr_analyze(mr_repo.root, "--gitlab-mr", "7", "--no-save", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["merge_request"]["target"] == "main"
    assert payload["merge_request"]["source"] == "feature/items"
    assert payload["gitlab"]["status"] == "ok"
    assert payload["gitlab"]["local"]["head_matches_server"] is True


def test_mr_analyze_refuses_an_unregistered_repository(tmp_path, kh_root, capsys):
    path = tmp_path / "unregistered"
    path.mkdir()
    assert mr_analyze(path, "--no-save") == 2
    assert "not a registered project" in capsys.readouterr().out
