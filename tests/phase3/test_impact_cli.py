"""`khunkhaar impact` as a shipped command: refusals first, then the report an operator reads.

The command consumes the artifact of the previous `git analyze`; it never invents a change set.
These tests drive the real CLI against synthetic registered repositories and assert the two
properties that matter operationally: the report is scoped to one project, and `--no-save` leaves
no impact rows or artifacts behind.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixture setup only, never exercised by KHUNKHAAR itself
from pathlib import Path

from core import cli
from core.impact import risk as impact_risk
from core.metadata import store


def register(path) -> int:
    return cli.main(["khunkhaar", "project", "register", "--path", str(path), "--json"])


def analyze(path, *flags: str) -> int:
    return cli.main(["khunkhaar", "git", "analyze", "--path", str(path), *flags])


def impact(path, *flags: str) -> int:
    return cli.main(["khunkhaar", "impact", "--path", str(path), *flags])


def _project(root: Path) -> dict:
    return next(item for item in store.list_projects()
                if Path(item["root_path"]).resolve() == root.resolve())


def _report_count(project_id: str) -> int:
    with store.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM impact_reports WHERE project_id=?",
                            (project_id,)).fetchone()["n"]


def _make_repo(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)  # nosec B603
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)  # nosec B603
    subprocess.run(["git", "-c", "user.email=qa@example.invalid", "-c", "user.name=QA",
                    "commit", "-qm", "initial"], cwd=root, check=True)  # nosec B603
    (root / "app" / "other.py").write_text(
        "class PaymentGateway:\n    def charge(self):\n        return 1\n\n# changed\n",
        encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)  # nosec B603
    subprocess.run(["git", "-c", "user.email=qa@example.invalid", "-c", "user.name=QA",
                    "commit", "-qm", "change the gateway"], cwd=root, check=True)  # nosec B603


def test_impact_refuses_an_unregistered_project(kh_root, impact_repo, capsys):
    rc = impact(impact_repo.root)
    out = capsys.readouterr().out
    assert rc == 2
    assert "not a registered project" in out
    assert store.list_projects() == []


def test_impact_refuses_without_a_saved_analysis(kh_root, impact_repo, capsys):
    assert register(impact_repo.root) == 0
    capsys.readouterr()
    rc = impact(impact_repo.root)
    out = capsys.readouterr().out
    assert rc == 2
    assert "no saved git analysis" in out and "git analyze" in out
    assert _report_count(_project(impact_repo.root)["project_id"]) == 0


def test_impact_json_reports_the_chain_and_saves_a_scoped_report(kh_root, impact_repo, capsys):
    assert register(impact_repo.root) == 0
    capsys.readouterr()
    assert analyze(impact_repo.root, "--base", impact_repo.base_sha, "--head", "HEAD") == 0
    capsys.readouterr()

    before = impact_repo.state()
    assert impact(impact_repo.root, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert impact_repo.state() == before, "the repository index and worktree are untouched"

    assert payload["analysis"]["kind"] == "range"
    assert payload["changeset"]["summary"]["files_changed"] == 1
    assert payload["graph"]["rebuilt"] is True
    seeds = {item["node_id"] for item in payload["impact"]["seeds"]}
    assert {"file:app/service.py", "class:app/service.py:ItemService",
            "symbol:app/service.py:ItemService.rename"} <= seeds

    surface = {(item["type"], item["name"]) for item in payload["impact"]["surface"]}
    assert ("api", "POST /items") in surface
    assert ("entity", "items") in surface
    assert [item["name"] for item in payload["impact"]["workflows"]] == ["items"]
    assert [item["path"] for item in payload["impact"]["tests"]] == ["tests/test_service.py"]

    assert payload["wording"] == impact_risk.WORDING
    factors = payload["risk"]["factors"]
    assert len(factors) == 6
    assert round(sum(item["contribution"] for item in factors), 2) == payload["risk"]["score"]

    artifact = Path(payload["artifact"])
    assert payload["saved"] is True
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
    text = artifact.read_text(encoding="utf-8")
    assert "smoke-value-not-a-real-secret" not in text, "the environment value is never read"
    recorded = json.loads(text)
    assert recorded["risk"]["score"] == payload["risk"]["score"]
    assert recorded["impact"]["summary"] == payload["impact"]["summary"]

    project = _project(impact_repo.root)
    report = store.latest_impact_report(project["project_id"])
    assert report["report_id"] == payload["report_id"]
    assert report["risk_score"] == payload["risk"]["score"]
    assert len(report["risk_factors"]) == 6
    assert report["artifact"] == payload["artifact"].split(str(kh_root))[-1].lstrip("/")
    assert store.impact_graph_info(project["project_id"])["nodes"] > 0


def test_impact_reuses_the_graph_until_rebuilt(kh_root, impact_repo, capsys):
    assert register(impact_repo.root) == 0
    assert analyze(impact_repo.root, "--base", impact_repo.base_sha, "--head", "HEAD") == 0
    capsys.readouterr()

    assert impact(impact_repo.root, "--json") == 0
    first = json.loads(capsys.readouterr().out)
    assert first["graph"]["rebuilt"] is True

    assert impact(impact_repo.root, "--json") == 0
    second = json.loads(capsys.readouterr().out)
    assert second["graph"]["rebuilt"] is False
    assert second["graph"]["stats"]["nodes"] == first["graph"]["stats"]["nodes"]
    assert second["risk"]["score"] == first["risk"]["score"]

    assert impact(impact_repo.root, "--json", "--rebuild-graph") == 0
    third = json.loads(capsys.readouterr().out)
    assert third["graph"]["rebuilt"] is True
    assert _report_count(_project(impact_repo.root)["project_id"]) == 3


def test_no_save_writes_no_impact_rows_or_artifacts(kh_root, impact_repo, capsys):
    assert register(impact_repo.root) == 0
    assert analyze(impact_repo.root, "--base", impact_repo.base_sha, "--head", "HEAD") == 0
    capsys.readouterr()
    project = _project(impact_repo.root)

    assert impact(impact_repo.root, "--json", "--no-save") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["saved"] is False
    assert payload["artifact"] is None and payload["report_id"] is None
    assert _report_count(project["project_id"]) == 0
    assert store.impact_graph_info(project["project_id"])["nodes"] == 0
    graph_json = Path(kh_root) / "intelligence" / "dependency-graphs"
    assert not list(graph_json.glob("*.json")), "the graph was not persisted either"


def test_two_projects_do_not_share_analyses_or_graphs(kh_root, impact_repo, tmp_path, capsys):
    assert register(impact_repo.root) == 0
    assert analyze(impact_repo.root, "--base", impact_repo.base_sha, "--head", "HEAD") == 0
    capsys.readouterr()
    first = _project(impact_repo.root)
    assert impact(impact_repo.root, "--json") == 0
    capsys.readouterr()
    first_nodes = store.impact_graph_info(first["project_id"])["nodes"]

    other_root = tmp_path / "other-app"
    other_root.mkdir()
    _make_repo(other_root, {"app/__init__.py": "", "app/other.py": "class PaymentGateway:\n"
                                                                  "    def charge(self):\n"
                                                                  "        return 1\n"})
    assert register(other_root) == 0
    capsys.readouterr()
    second = _project(other_root)

    # The first project's analysis is not visible to the second: a graph cannot be inferred from
    # another repository's change set.
    assert impact(other_root) == 2
    assert "no saved git analysis" in capsys.readouterr().out

    assert analyze(other_root, "--base", "HEAD~1", "--head", "HEAD") == 0
    capsys.readouterr()
    assert impact(other_root, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] == second["project_id"]
    seeds = {item["node_id"] for item in payload["impact"]["seeds"]}
    assert "file:app/other.py" in seeds
    assert not any("ItemService" in node_id for node_id in payload["impact"]["summary"]
                   .get("impacted_by_type", {})), "the first project's graph is not consulted"

    names = [node["name"] for node in store.load_impact_graph(second["project_id"])["nodes"]]
    assert "PaymentGateway" in names and "ItemService" not in names
    assert store.impact_graph_info(first["project_id"])["nodes"] == first_nodes
