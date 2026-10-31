"""Module A correctness: what the analysis says about a repository it is only looking at.

Every assertion here is against a real git repository built by the fixture, so "staged", "renamed",
"deleted", "conflicted" and "merge commit" are git's own answers, read back. Where git itself can
only guess - the enclosing symbol of a hunk - the test asserts that the guess is labelled instead
of being presented as fact.
"""

from __future__ import annotations

import pytest

from core.git import analysis
from core.git.analysis import (branch_facts, parse_name_status, parse_numstat, parse_patch,
                               parse_status, unquote)
from core.git.runner import GitError, GitRepository, open_repository

FEATURE_COMMITS = ["change target signature and rename legacy", "add the scratch file",
                   "delete the scratch file", "add a binary asset", "reflow the readme"]


def repo_for(fixture) -> GitRepository:
    return open_repository(fixture.root)


# ------------------------------------------------------------------ parsers
def test_parse_status_separates_the_three_questions(dirty_repo):
    state = parse_status(dirty_repo_state_lines(dirty_repo))
    assert state.staged == ["src/app.py"]
    assert state.unstaged == ["cfg.yaml"]
    assert state.untracked == ["scratch.txt"]
    assert state.conflicted == []
    assert not state.clean


def dirty_repo_state_lines(fixture):
    return GitRepository(fixture.root).status_porcelain()


def test_parse_status_reads_a_staged_rename_from_its_record():
    lines = ["2 R. N... 100644 100644 100644 0f41 0f41 R100 current.txt\tlegacy.txt",
             "1 M. N... 100644 100644 100644 37db 5dd5 src/app.py",
             "1 .M N... 100644 100644 100644 2680 2680 cfg.yaml",
             "u UU N... 100644 100644 100644 100644 aaa bbb ccc cfg.yaml",
             "? fresh.txt", "! ignored.txt", "# branch.head main"]
    state = parse_status(lines)
    assert state.staged == ["current.txt", "src/app.py"]
    assert state.unstaged == ["cfg.yaml"]
    assert state.untracked == ["fresh.txt"]
    assert state.conflicted == ["cfg.yaml"]
    assert state.ignored_count == 1


def test_parse_status_handles_a_path_that_git_decides_to_quote():
    """core.quotePath makes non-ASCII names arrive as `"...\\303\\251..."`."""
    assert unquote('"caf\\303\\251.py"') == "café.py"
    assert unquote("plain.py") == "plain.py"
    assert unquote('"tab\\there"') == "tab\there"


def test_parse_name_status_reads_rename_pairs():
    parsed = parse_name_status(["R100\tlegacy.txt\tcurrent.txt", "M\tsrc/app.py",
                                "A\tbin.dat", "D\tgone.txt"])
    assert parsed["current.txt"] == ("R", "legacy.txt")
    assert parsed["src/app.py"] == ("M", None)
    assert parsed["gone.txt"] == ("D", None)


def test_parse_numstat_reads_binary_as_a_change_without_counts():
    parsed = parse_numstat(["3\t1\tsrc/app.py", "-\t-\tbin.dat", "0\t0\tlegacy.txt => current.txt"])
    assert parsed["src/app.py"] == (3, 1)
    assert parsed["bin.dat"] == (0, 0)
    assert "current.txt" in parsed


def test_parse_patch_collects_hunks_and_symbols():
    patch = "\n".join([
        "diff --git a/src/app.py b/src/app.py",
        "index 37dbf7c..5dd506c 100644",
        "--- a/src/app.py",
        "+++ b/src/app.py",
        "@@ -5 +5,3 @@ def keep():",
        "-def target(a):",
        "+def target(a, b=2):",
        "+    if b:",
        "@@ -20 +22 @@ class Other:",
        "+        return a",
        "diff --git a/README.md b/README.md",
        "--- a/README.md",
        "+++ b/README.md",
        "@@ -3 +3,2 @@",
        "+middle",
    ])
    details = parse_patch(patch)
    assert details["src/app.py"].hunks == 2
    assert details["src/app.py"].contexts == ["def keep():", "class Other:"]
    assert details["src/app.py"].added == 3
    assert details["src/app.py"].removed == 1
    assert details["src/app.py"].lines_capped is False
    # Attribution needs the line numbers, numbered in the file each side actually uses.
    assert details["src/app.py"].old_lines == [5]
    assert details["src/app.py"].new_lines == [5, 6, 22]
    assert details["README.md"].contexts == []


def test_branch_facts_reads_the_porcelain_header():
    facts = branch_facts(["# branch.oid abc123", "# branch.head main",
                         "# branch.upstream origin/main", "# branch.ab +2 -1"])
    assert facts == {"oid": "abc123", "head": "main", "upstream": "origin/main", "ab": "+2 -1"}


# ------------------------------------------------------------------ change sets
def test_feature_branch_change_set(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), base=git_repo.base_sha,
                            head="feature/payments")
    by_path = {f.path: f for f in set_.files}
    assert set_.kind == "range"
    assert set_.base == git_repo.base_sha or set_.base_source == "explicit"
    assert by_path["src/app.py"].status == "modified"
    assert by_path["src/app.py"].insertions > 0
    assert by_path["current.txt"].status == "renamed"
    assert by_path["current.txt"].old_path == "legacy.txt"
    assert "gone.txt" not in by_path, "added then deleted is no change against the base"
    assert by_path["bin.dat"].status == "added"
    assert by_path["bin.dat"].binary is True
    assert len(set_.commits) == len(FEATURE_COMMITS)
    assert [c["subject"] for c in set_.commits][0] == FEATURE_COMMITS[-1], "newest first"


def test_a_deleted_file_is_reported_by_the_commit_that_deleted_it(git_repo):
    repo = repo_for(git_repo)
    set_ = analysis.analyze(repo, commit=git_repo.shas["delete the scratch file"])
    assert [f.path for f in set_.files] == ["gone.txt"]
    assert set_.files[0].status == "deleted"
    assert set_.summary()["removed"] == 1


def test_a_root_commit_is_every_file(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), commit=git_repo.base_sha)
    assert {f.status for f in set_.files} == {"added"}
    assert any("root commit" in note for note in set_.notes)
    assert set_.base == "(no parent)"


def test_a_merge_commit_is_attributed_to_its_first_parent(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), commit=git_repo.merge_sha)
    assert any("first parent" in note for note in set_.notes)
    assert set_.summary()["merge_commits"] == 1
    by_path = {f.path: f for f in set_.files}
    assert by_path["current.txt"].status == "renamed"
    assert "cfg.yaml" not in by_path, "the merge did not change what main already had"


def test_working_tree_keeps_staged_and_unstaged_apart(dirty_repo):
    set_ = analysis.analyze(repo_for(dirty_repo), working_tree=True)
    by_path = {f.path: f for f in set_.files}
    assert by_path["src/app.py"].staged and not by_path["src/app.py"].unstaged
    assert by_path["cfg.yaml"].unstaged and not by_path["cfg.yaml"].staged
    assert by_path["scratch.txt"].status == "untracked"
    assert by_path["scratch.txt"].untracked
    assert by_path["src/app.py"].functions, "a python edit is attributed to its function"


def test_conflict_state_is_read_from_the_index(conflicted_repo):
    repo = repo_for(conflicted_repo)
    state = analysis.worktree(repo)
    assert state.conflicted == ["cfg.yaml"]
    set_ = analysis.analyze(repo, working_tree=True)
    assert set_.conflicted is True
    assert not set_.worktree_clean


def test_a_clean_repository_reports_no_worktree_change(git_repo):
    repo = repo_for(git_repo)
    set_ = analysis.analyze(repo, working_tree=True)
    assert set_.files == []
    assert set_.worktree_clean and set_.conflicted is False


def test_base_is_guessed_from_config_candidates_when_not_given(git_repo):
    info = analysis.identity(repo_for(git_repo))
    assert info.base_branch == "main"
    assert info.base_source == "candidate"


def test_an_explicit_base_is_labelled_as_such(git_repo):
    info = analysis.identity(repo_for(git_repo), base=git_repo.base_sha)
    assert info.base_source == "explicit"


def test_an_unknown_base_is_refused(git_repo):
    with pytest.raises(GitError, match="does not resolve"):
        analysis.identity(repo_for(git_repo), base="no-such-branch")


def test_without_any_candidate_there_is_no_base_to_guess(git_repo, patch_config):
    patch_config("git", {"base_branch_candidates": []})
    with pytest.raises(GitError, match="no comparison base"):
        analysis.analyze(repo_for(git_repo))


def test_branch_analysis_names_the_divergence_point(git_repo):
    """main already contains the branch, so the branch itself has added nothing further."""
    set_ = analysis.analyze(repo_for(git_repo), branch="feature/payments",
                            base=git_repo.base_sha)
    assert set_.kind == "branch"
    assert [c["subject"] for c in set_.commits] == list(reversed(FEATURE_COMMITS))
    assert any("diverged" in note for note in set_.notes)


def test_range_two_dot_and_three_dot_are_different_claims(git_repo):
    repo = repo_for(git_repo)
    two = analysis.analyze(repo, range_spec="main...feature/payments".replace("...", ".."))
    three = analysis.analyze(repo, range_spec="main...feature/payments")
    assert any("two-dot" in note for note in two.notes)
    assert any("three-dot" in note for note in three.notes)
    assert {f.path for f in three.files} == set(), "feature added nothing beyond the merge base"
    assert {f.path for f in two.files}


def test_detached_head_is_reported_as_detached(git_repo):
    git_repo.git("checkout", "-q", "--detach", git_repo.base_sha)
    info = analysis.identity(repo_for(git_repo))
    assert info.detached is True
    assert info.branch in ("(detached)", "(branch: detached)")
    assert info.head == git_repo.base_sha


def test_identity_reports_a_paused_rebase_not_just_a_detached_head(rebasing_repo):
    info = analysis.identity(repo_for(rebasing_repo))
    assert info.operation == "rebase"
    assert info.detached is True
    assert info.conflicted is True


def test_identity_reports_a_merge_conflict(conflicted_repo):
    info = analysis.identity(repo_for(conflicted_repo))
    assert info.operation == "merge"
    assert info.conflicted is True


def test_a_repository_that_is_not_mid_operation_says_none(git_repo):
    info = analysis.identity(repo_for(git_repo), base=git_repo.base_sha)
    assert info.operation is None


def test_identity_counts_commits_and_reports_the_branch(git_repo):
    info = analysis.identity(repo_for(git_repo), base=git_repo.base_sha)
    assert info.branch == "main"
    assert info.commit_count == len(git_repo.git("rev-list", "HEAD").stdout.split())
    assert info.worktree_clean is True
    assert info.conflicted is False
    assert info.reads > 0


def test_a_type_with_no_parser_keeps_gits_label_and_says_so(git_repo):
    """git prints the preceding line for these types; calling that a function would be a guess."""
    set_ = analysis.analyze(repo_for(git_repo), base=git_repo.base_sha, head="feature/payments")
    readme = next(f for f in set_.files if f.path == "README.md")
    assert readme.hunks > 0
    assert readme.functions == []
    assert readme.attribution == "none"
    assert readme.hunk_contexts, "git's own label is preserved rather than discarded"
    assert any("no definition was named for" in note for note in set_.notes)


def test_python_hunks_are_attributed_to_their_declarations(git_repo):
    """The parsed answer replaces git's guess, and git's guess is still kept alongside it."""
    set_ = analysis.analyze(repo_for(git_repo), base=git_repo.base_sha,
                            head="feature/payments")
    app = next(f for f in set_.files if f.path == "src/app.py")
    assert app.attribution == "python-ast"
    assert app.functions == ["target"]
    assert [s["change"] for s in app.symbols] == ["modified"]
    assert app.hunk_contexts == ["def keep():"], "the line above the hunk, not the changed one"
    assert app.hunk_contexts != app.functions


# ------------------------------------------------------------------ symbol attribution
def test_attribution_separates_modified_added_and_removed_definitions(py_repo):
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas[
        "edit definitions and rename one away"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    assert store.attribution == "python-ast"
    assert {s["name"]: s["change"] for s in store.symbols} == {
        "Store.load.strip_chunk": "modified", "Store.save": "modified",
        "unused": "removed", "flush": "added",
    }


def test_a_change_inside_a_method_is_not_credited_to_the_class(py_repo):
    """`Store` spans the whole edit, but no changed line is in the class body itself."""
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas[
        "edit definitions and rename one away"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    names = {s["name"] for s in store.symbols}
    assert "Store" not in names
    assert "Store.load" not in names, "only the nested helper changed, not its parent"
    kinds = {s["name"]: s["kind"] for s in store.symbols}
    assert kinds["Store.load.strip_chunk"] == "function"


def test_a_decorator_line_is_attributed_to_the_function_it_decorates(py_repo):
    """`ast` numbers the def, not the decorator; the definition starts where the decorator does."""
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas["change only a decorator"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    assert [(s["name"], s["change"], s["start"]) for s in store.symbols] == [
        ("Store.load", "modified", 8)]


def test_a_root_commit_is_every_definition_added(py_repo):
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas["add store"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    assert store.attribution == "python-ast"
    assert {s["name"] for s in store.symbols} == {
        "Store", "Store.__init__", "Store.load", "Store.load.strip_chunk", "Store.save", "unused"}
    assert all(s["change"] == "added" for s in store.symbols)


def test_an_untracked_python_file_is_attributed_as_additions(py_repo):
    """Reading the worktree side of a new file still has to leave the index alone."""
    py_repo.write("src/fresh.py", "def brand_new(x):\n    return x\n")
    before = py_repo.state()
    set_ = analysis.analyze(repo_for(py_repo), working_tree=True)
    fresh = next(f for f in set_.files if f.path == "src/fresh.py")
    assert fresh.untracked is True
    assert [(s["name"], s["change"]) for s in fresh.symbols] == [("brand_new", "added")]
    assert py_repo.state() == before


def test_a_python_file_that_does_not_parse_is_unknown_not_empty(py_repo):
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas[
        "add a file that is not valid python 3"])
    legacy = next(f for f in set_.files if f.path == "templates/legacy.py")
    assert legacy.language == "python"
    assert legacy.attribution != "python-ast"
    assert legacy.symbols == []
    assert legacy.functions == []
    assert any("could not be attributed" in note for note in set_.notes)
    assert any("unknown, not absent" in note for note in set_.notes)


def test_attribution_is_a_config_switch(py_repo, patch_config):
    """With the parser off, the report falls back to git's label - and says that it did.

    Git's python pattern only matches a definition at column 1, so every change inside a method is
    labelled with the class. That is the case the `ast` pass exists for.
    """
    patch_config("git", {"ast_symbol_attribution": False})
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas[
        "edit definitions and rename one away"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    assert store.attribution == "git-hunk-header"
    assert store.symbols == []
    assert store.hunks == 3, "three hunks, one label: git names the class, not the methods"
    assert store.functions == ["class Store:"]
    assert any("hunk-header label" in note for note in set_.notes)


def test_the_attribution_budget_leaves_the_rest_unparsed(py_repo, patch_config):
    patch_config("git", {"max_symbol_attribution_files": 0})
    set_ = analysis.analyze(repo_for(py_repo), commit=py_repo.shas[
        "edit definitions and rename one away"])
    store = next(f for f in set_.files if f.path == "src/store.py")
    assert store.symbols == []
    assert any("max_symbol_attribution_files" in note for note in set_.notes)


def test_reading_the_python_history_leaves_the_repository_alone(py_repo):
    fixture = py_repo
    repo = repo_for(fixture)
    before = fixture.state()
    for kwargs in ({"commit": fixture.shas["edit definitions and rename one away"]},
                   {"base": fixture.base_sha, "head": "HEAD"},
                   {"working_tree": True}):
        analysis.analyze(repo, **kwargs)
    assert fixture.state() == before



def test_the_file_list_respects_the_configured_limit(git_repo, patch_config):
    patch_config("git", {"max_files_in_report": 2})
    set_ = analysis.analyze(repo_for(git_repo), commit=git_repo.base_sha)
    assert set_.truncated is True
    assert len(set_.files) == 2
    assert any("max_files_in_report" in note for note in set_.notes)


def test_binary_files_are_flagged_and_explained(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), commit=git_repo.shas["add a binary asset"])
    binary = set_.files[0]
    assert binary.binary is True
    assert binary.insertions == 0 and binary.deletions == 0
    assert any("binary" in note for note in set_.notes)


def test_language_is_reported_per_file(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), commit=git_repo.shas["add a binary asset"])
    assert set_.files[0].language is None
    set_ = analysis.analyze(repo_for(git_repo), base=git_repo.base_sha, head="feature/payments")
    assert {f.path: f.language for f in set_.files}["src/app.py"] == "python"


# ------------------------------------------------------------------ commit history
def test_each_commit_carries_the_files_it_changed(git_repo):
    set_ = analysis.analyze(repo_for(git_repo), branch="feature/payments", base=git_repo.base_sha)
    by_subject = {c["subject"]: c for c in set_.commits}
    change = by_subject["change target signature and rename legacy"]
    assert change["files_changed"] == 2
    assert {"status": "M", "path": "src/app.py", "old_path": None} in change["files"]
    assert {"status": "R100", "path": "current.txt",
            "old_path": "legacy.txt"} in change["files"]
    assert by_subject["delete the scratch file"]["files"] == [
        {"status": "D", "path": "gone.txt", "old_path": None}]
    assert by_subject["add a binary asset"]["files"][0]["path"] == "bin.dat"


def test_a_merge_commit_has_no_file_list_and_that_is_said_out_loud(git_repo):
    """git does not diff a merge against either parent unless told which; guessing one is a lie."""
    set_ = analysis.analyze(repo_for(git_repo), base=git_repo.base_sha, head="main")
    merge = next(c for c in set_.commits if c["is_merge"])
    assert merge["files"] == [] and merge["files_changed"] == 0
    assert any("merge commit(s) carry no file list" in note for note in set_.notes)


def test_commit_trailers_are_read_from_the_last_paragraph(git_repo):
    git_repo.git("checkout", "-q", "feature/payments")
    git_repo.git("commit", "-q", "--allow-empty", "-m", "prepare the release", "-m",
                 "Signed-off-by: QA Fixture <qa@example.invalid>\n"
                 "Co-authored-by: Rhea <rhea@example.invalid>\n"
                 "Co-authored-by: Sam <sam@example.invalid>")
    set_ = analysis.analyze(repo_for(git_repo), commit="HEAD")
    newest = set_.commits[0]
    assert newest["subject"] == "prepare the release"
    assert newest["signed_off"] is True
    assert newest["trailers"]["signed-off-by"] == ["QA Fixture <qa@example.invalid>"]
    assert newest["co_authors"] == ["Rhea <rhea@example.invalid>", "Sam <sam@example.invalid>"]


def test_a_body_that_merely_mentions_a_trailer_is_not_one(git_repo):
    git_repo.git("checkout", "-q", "feature/payments")
    git_repo.git("commit", "-q", "--allow-empty", "-m", "note the policy", "-m",
                 "This fixes: the login loop\nand also documents the timeout")
    set_ = analysis.analyze(repo_for(git_repo), commit="HEAD")
    assert set_.commits[0]["trailers"] == {}
    assert set_.commits[0]["signed_off"] is False


def test_per_commit_file_lists_can_be_switched_off(git_repo, patch_config):
    patch_config("git", {"commit_file_lists": False})
    set_ = analysis.analyze(repo_for(git_repo), branch="feature/payments", base=git_repo.base_sha)
    assert set_.commits and all("files" not in commit for commit in set_.commits)


def test_the_per_commit_file_cap_is_reported_on_the_commit(git_repo, patch_config):
    patch_config("git", {"max_files_per_commit": 1})
    set_ = analysis.analyze(repo_for(git_repo), branch="feature/payments", base=git_repo.base_sha)
    change = next(c for c in set_.commits
                  if c["subject"] == "change target signature and rename legacy")
    assert change["files_changed"] == 2
    assert len(change["files"]) == 1
    assert change["files_truncated"] is True


# ------------------------------------------------------------------ the headline guarantee
def test_analysis_never_touches_the_repository(dirty_repo):
    """The whole point: reading a company checkout must leave it exactly as it was."""
    fixture = dirty_repo
    repo = repo_for(fixture)
    before = fixture.state()
    for kwargs in ({"base": fixture.base_sha, "head": "feature/payments"},
                   {"working_tree": True},
                   {"commit": fixture.merge_sha},
                   {"branch": "feature/payments", "base": fixture.base_sha},
                   {"range_spec": "HEAD~2..HEAD"}):
        analysis.analyze(repo, **kwargs)
    analysis.identity(repo)
    analysis.worktree(repo)
    after = fixture.state()
    assert after == before, ("git index, file contents or mtimes changed during a read - "
                             "the analysis would have been invalid anyway")
    assert repo.reads > 20, "the reads actually happened"


def test_a_stale_analysis_aborts_rather_than_describing_a_moved_repository(git_repo, monkeypatch):
    calls = {"n": 0}
    original = GitRepository.snapshot

    def drifting(self):
        calls["n"] += 1
        return original(self) if calls["n"] != 2 else ("someone committed", "deadbeef")

    monkeypatch.setattr(GitRepository, "snapshot", drifting)
    with pytest.raises(Exception, match="repository state changed"):
        analysis.analyze(GitRepository(git_repo.root), working_tree=True)
