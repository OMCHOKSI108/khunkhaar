"""`khunkhaar git status|analyze` as a shipped command, not only as a library call.

The safety rules the module is built around are only real if the CLI enforces them: an
unregistered repository is refused before git is invoked, a malformed ref never reaches a
subprocess, and the report that a user reads is the same report the artifact records.
"""

from __future__ import annotations

import json

import pytest

from core import cli
from core.metadata import store


def register(path) -> int:
    return cli.main(["khunkhaar", "project", "register", "--path", str(path), "--json"])


def test_git_status_reads_a_registered_repository(git_repo, kh_root, capsys):
    assert register(git_repo.root) == 0
    capsys.readouterr()
    assert cli.main(["khunkhaar", "git", "status", "--path", str(git_repo.root)]) == 0
    out = capsys.readouterr().out
    assert "main" in out
    assert git_repo.merge_sha[:10] in out


def test_git_status_refuses_an_unregistered_repository(kh_root, tmp_path, capsys):
    other = tmp_path / "never-registered"
    (other / ".git").mkdir(parents=True)
    assert cli.main(["khunkhaar", "git", "status", "--path", str(other)]) == 2
    assert "not a registered project" in capsys.readouterr().out
    assert store.list_projects() == []


def test_analyze_prints_the_parsed_symbol_and_labels_its_source(py_repo, kh_root, capsys):
    assert register(py_repo.root) == 0
    capsys.readouterr()
    rc = cli.main(["khunkhaar", "git", "analyze", "--path", str(py_repo.root),
                   "--commit", py_repo.shas["edit definitions and rename one away"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Store.load.strip_chunk" in out
    assert "[python-ast]" in out
    assert "[git hunk header" not in out, "the parsed answer replaces the heuristic, not beside it"


def test_analyze_json_carries_symbols_and_the_untouched_flag(py_repo, kh_root, capsys):
    assert register(py_repo.root) == 0
    capsys.readouterr()
    rc = cli.main(["khunkhaar", "git", "analyze", "--path", str(py_repo.root),
                   "--base", py_repo.base_sha, "--head", "HEAD", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    files = {f["path"]: f for f in payload["changeset"]["files"]}
    assert files["src/store.py"]["attribution"] == "python-ast"
    assert {s["name"] for s in files["src/store.py"]["symbols"]} >= {
        "Store.load", "Store.load.strip_chunk", "Store.save"}
    assert payload["index_untouched"] is True
    assert files["templates/legacy.py"]["symbols"] == []
    assert files["templates/legacy.py"]["attribution"] != "python-ast"
    commits = payload["changeset"]["commits"]
    assert commits[0]["subject"] == "add a file that is not valid python 3"
    assert commits[0]["files"] == [
        {"status": "A", "path": "templates/legacy.py", "old_path": None}]
    assert commits[0]["signed_off"] is False


def test_analyze_persists_an_artifact_and_counts_only_rows(py_repo, kh_root, capsys):
    assert register(py_repo.root) == 0
    capsys.readouterr()
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(py_repo.root),
                     "--commit", py_repo.shas["edit definitions and rename one away"],
                     "--save"]) == 0
    capsys.readouterr()
    project = store.list_projects()[0]
    rows = store.git_analyses(project["project_id"])
    assert len(rows) == 1
    assert rows[0]["files_changed"] == 1
    assert rows[0]["index_untouched"] == 1
    assert rows[0]["kind"] == "commit"

    artifact = (kh_root / rows[0]["artifact"]).resolve()
    assert artifact.is_file(), rows[0]["artifact"]
    assert artifact.stat().st_mode & 0o777 == 0o600, "the artifact names repository content"
    recorded = json.loads(artifact.read_text(encoding="utf-8"))
    assert {f["path"] for f in recorded["changeset"]["files"]} == {"src/store.py"}
    assert recorded["changeset"]["files"][0]["attribution"] == "python-ast"
    # The row holds counts only: no path from the repository appears in the metadata table.
    assert all(not str(value).startswith("/") for value in rows[0].values()
               if isinstance(value, str))


def test_the_database_row_never_copies_the_file_list(py_repo, kh_root, capsys):
    assert register(py_repo.root) == 0
    capsys.readouterr()
    cli.main(["khunkhaar", "git", "analyze", "--path", str(py_repo.root),
              "--base", py_repo.base_sha, "--save"])
    capsys.readouterr()
    project = store.list_projects()[0]
    blob = json.dumps(store.git_analyses(project["project_id"]))
    assert "src/store.py" not in blob
    assert "templates/legacy.py" not in blob


def test_a_malformed_ref_is_refused_before_git_runs(git_repo, kh_root, capsys):
    assert register(git_repo.root) == 0
    capsys.readouterr()
    rc = cli.main(["khunkhaar", "git", "analyze", "--path", str(git_repo.root),
                   "--base", "HEAD;rm -rf /"])
    assert rc == 1
    assert "unsupported characters" in capsys.readouterr().out
    assert store.git_analyses(store.list_projects()[0]["project_id"]) == []


def test_two_comparison_modes_at_once_is_a_usage_error(git_repo, kh_root, capsys):
    assert register(git_repo.root) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["khunkhaar", "git", "analyze", "--path", str(git_repo.root),
                  "--base", "main", "--commit", git_repo.merge_sha])


def test_working_tree_analysis_does_not_stage_anything(dirty_repo, kh_root, capsys):
    assert register(dirty_repo.root) == 0
    capsys.readouterr()
    before = dirty_repo.state()
    assert cli.main(["khunkhaar", "git", "analyze", "--path", str(dirty_repo.root),
                     "--working-tree"]) == 0
    assert dirty_repo.state() == before
    out = capsys.readouterr().out
    app = next(line for line in out.splitlines() if line.rstrip().endswith("src/app.py"))
    cfg = next(line for line in out.splitlines() if line.rstrip().endswith("cfg.yaml"))
    assert "S..." in app, "the staged edit is labelled staged"
    assert ".W.." in cfg, "the unstaged edit is a different answer"
    assert "target" in out and "[python-ast]" in out


def test_git_command_is_listed_in_help(kh_root, capsys):
    assert cli.main(["khunkhaar", "--help"]) == 0
    assert "git" in capsys.readouterr().out
