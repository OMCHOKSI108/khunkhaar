"""Module A safety envelope: what the read-only git runner refuses, and what it proves.

These tests are the executable version of "Never alter the user's Git index or working tree" and
"Do not let an LLM-generated command execute arbitrary shell operations without validation". They
run against a real repository built in tmp_path, so a passing test means git itself was refused or
was observed to be unchanged - not that a mock agreed with us.
"""

from __future__ import annotations

import pytest

from core.git import runner as git_runner
from core.git.runner import (GitError, GitRepository, RepositoryUntrustedError,
                             StateChangedError, open_repository, redact_url, validate_path,
                             validate_ref, validate_range)

WRITE_COMMANDS = ["commit", "checkout", "switch", "restore", "reset", "clean", "stash", "merge",
                  "rebase", "cherry-pick", "push", "pull", "fetch", "clone", "apply", "am",
                  "rm", "mv", "add", "branch", "tag", "worktree", "gc", "revert", "bisect",
                  "replace", "notes", "update-ref", "symbolic-ref", "write-tree", "prune"]


def test_every_write_capable_subcommand_is_refused(git_repo):
    repo = GitRepository(git_repo.root)
    for subcommand in WRITE_COMMANDS:
        with pytest.raises(GitError, match="not on the read-only allowlist"):
            repo.run(subcommand)
    assert repo.reads == 0, "a refused command must never reach subprocess at all"


def test_open_repository_refuses_a_directory_that_is_not_a_worktree(non_repo):
    with pytest.raises(RepositoryUntrustedError):
        open_repository(non_repo)


def test_open_repository_refuses_a_missing_directory(tmp_path):
    with pytest.raises(RepositoryUntrustedError, match="is not a directory"):
        open_repository(tmp_path / "does-not-exist")


@pytest.mark.parametrize("evil", [
    "--help", "--output=/tmp/pwned", "-C", "; rm -rf /", "$(whoami)", "`id`", "a|b", "a b",
    "--", "../outside", "HEAD;echo", "a\nb", "", "   ", "x" * 5000, "--end-of-options",
])
def test_a_ref_that_is_not_a_plain_name_is_refused(evil):
    with pytest.raises(GitError):
        validate_ref(evil)


@pytest.mark.parametrize("good", ["HEAD", "main", "feature/payments", "origin/main",
                                  "HEAD~1", "HEAD^", "abcdef1234567890abcdef1234567890abcdef12",
                                  "v1.2.3", "HEAD:src/app.py", "release_2026-04-01", "@{u}"])
def test_a_real_revspec_is_accepted(good):
    assert validate_ref(good) == good.strip()


def test_blob_reference_cannot_escape_the_repository():
    for evil in ["HEAD:../../etc/passwd", "HEAD:/etc/passwd", "HEAD:a:b", "HEAD:"]:
        with pytest.raises(GitError):
            validate_ref(evil)


@pytest.mark.parametrize("evil", ["", "-", "-x", "a/..", "/abs", "..", "../x", "x" * 5000,
                                  "back\\slash"])
def test_a_pathspec_cannot_escape_or_look_like_a_flag(evil):
    with pytest.raises(GitError):
        validate_path(evil)


def test_range_parsing_accepts_two_and_three_dot_forms():
    assert validate_range("main..feature") == ("main", "feature")
    assert validate_range("main...feature") == ("main", "feature")
    with pytest.raises(GitError):
        validate_range("main")
    with pytest.raises(GitError):
        validate_range("--help..HEAD")


def test_end_of_options_is_used_for_rev_taking_commands(git_repo):
    repo = GitRepository(git_repo.root)
    argv = repo._base_argv("diff", ["--name-status"], ["HEAD~1", "HEAD"], None)
    assert "--end-of-options" in argv
    assert argv[:5] == ["git", "--no-optional-locks", "--no-pager", "-C", str(git_repo.root)]


def test_rev_parse_is_called_without_end_of_options_because_it_echoes_it(git_repo):
    """`git rev-parse --end-of-options HEAD` prints the separator back; the guard would corrupt it.

    The rev is still validated, and a validated rev cannot start with '-', which is what the
    separator exists to protect against.
    """
    repo = GitRepository(git_repo.root)
    argv = repo._base_argv("rev-parse", ["--verify", "--quiet"], ["HEAD"], None)
    assert "--end-of-options" not in argv
    assert repo.verify_ref("HEAD") == git_repo.sha()


def test_a_ref_cannot_be_smuggled_in_as_an_option(git_repo):
    repo = GitRepository(git_repo.root)
    with pytest.raises(GitError, match="must start with '-'"):
        repo.run("status", options=["--porcelain=v2", "HEAD"])


def test_configuration_can_narrow_the_allowlist_but_never_widen_it(git_repo, patch_config):
    patch_config("git", {"read_only_commands": ["status", "push", "commit", "reset"]})
    repo = GitRepository(git_repo.root)
    assert "status" in repo.allowlist
    for forbidden in ("push", "commit", "reset"):
        assert forbidden not in repo.allowlist
        with pytest.raises(GitError, match="not on the read-only allowlist"):
            repo.run(forbidden)


def test_index_and_working_tree_are_unchanged_after_a_full_read(git_repo, dirty_repo):
    for fixture in (git_repo, dirty_repo):
        repo = GitRepository(fixture.root)
        before = fixture.state()
        repo.run("status", options=["--porcelain=v2"])
        repo.diff_name_status("HEAD~2", "HEAD")
        repo.diff_patch("HEAD~2", "HEAD")
        repo.commits("HEAD", base="HEAD~2")
        repo.blob("HEAD:README.md")
        assert fixture.state() == before


def test_a_changed_snapshot_aborts_the_read(git_repo, monkeypatch):
    calls = {"n": 0}
    original = GitRepository.snapshot

    def drifting(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("before", "head")
        return original(self)

    monkeypatch.setattr(GitRepository, "snapshot", drifting)
    repo = GitRepository(git_repo.root)
    with pytest.raises(StateChangedError, match="repository state changed"):
        repo.run("status")


def test_failure_is_reported_even_when_the_repository_moved_first(git_repo, monkeypatch):
    """The invariance check runs before the exit status so a crash cannot hide a mutation."""
    original = GitRepository.snapshot
    calls = {"n": 0}

    def drifting(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("before", "head")
        return original(self)

    monkeypatch.setattr(GitRepository, "snapshot", drifting)
    repo = GitRepository(git_repo.root)
    with pytest.raises(StateChangedError):
        repo.run("log", options=["--max-count=1"], revs=["no-such-ref"], check=True)


def test_credentials_in_a_remote_url_never_reach_the_report(git_repo):
    git_repo.git("remote", "add", "origin",
                 "https://oauth2:glpat-secret@git.example.invalid/org/app.git")
    repo = GitRepository(git_repo.root)
    url = repo.remote_url()
    assert url is not None
    assert "glpat-secret" not in url
    assert "oauth2" not in url
    assert url.endswith("git.example.invalid/org/app.git")


def test_ssh_remote_without_credentials_is_left_readable(git_repo):
    git_repo.git("remote", "add", "origin", "git@git.example.invalid:org/app.git")
    assert GitRepository(git_repo.root).remote_url() == "git@git.example.invalid:org/app.git"


@pytest.mark.parametrize("url,expected", [
    ("https://user:token@host/x.git", "https://<redacted>@host/x.git"),
    ("https://host/x.git", "https://host/x.git"),
    ("ssh://git@host/x.git", "ssh://<redacted>@host/x.git"),
    ("git@host:org/x.git", "git@host:org/x.git"),
    ("/srv/local/repo.git", "/srv/local/repo.git"),
])
def test_redact_url(url, expected):
    assert redact_url(url) == expected


def test_remote_name_is_validated_before_it_becomes_a_config_key(git_repo):
    repo = GitRepository(git_repo.root)
    for evil in ["origin; rm -rf /", "../x", "", "a" * 65]:
        with pytest.raises(GitError):
            repo.remote_url(evil)


def test_a_command_that_fails_raises_with_gits_own_words(git_repo):
    repo = GitRepository(git_repo.root)
    with pytest.raises(GitError, match="git log failed"):
        repo.run("log", revs=["definitely-not-a-ref"])


def test_check_false_returns_the_failure_instead_of_raising(git_repo):
    repo = GitRepository(git_repo.root)
    result = repo.run("log", revs=["definitely-not-a-ref"], check=False)
    assert not result.ok
    assert result.returncode != 0


def test_timeout_message_names_the_subcommand(git_repo, monkeypatch):
    def fake_run(*args, **kwargs):
        raise git_runner.subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(git_runner.subprocess, "run", fake_run)
    repo = GitRepository(git_repo.root)
    repo.verify_untouched = False
    with pytest.raises(GitError, match="git diff timed out"):
        repo.run("diff", revs=["HEAD"])


def test_a_missing_git_binary_is_reported_as_unusable(git_repo, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git: not found")

    monkeypatch.setattr(git_runner.subprocess, "run", fake_run)
    repo = GitRepository(git_repo.root)
    repo.verify_untouched = False
    with pytest.raises(GitError, match="git is not usable"):
        repo.run("status")


def test_the_environment_cannot_smuggle_options_or_credentials(git_repo, monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    env = git_runner._env()
    assert "GIT_SSH_COMMAND" not in env
    assert "GIT_ASKPASS" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_PAGER"] == "cat"


def test_branch_listing_is_local_only(git_repo):
    repo = GitRepository(git_repo.root)
    names = repo.branch_names()
    assert "main" in names and "feature/payments" in names
    assert all(not name.startswith("refs/") for name in names)


def test_commit_metadata_and_merge_detection(git_repo):
    repo = GitRepository(git_repo.root)
    commits = repo.commits("feature/payments", base=git_repo.base_sha)
    subjects = [c["subject"] for c in commits]
    assert len(commits) == 5, "five commits sit between the base and the feature tip"
    assert len(subjects) == len(set(subjects))
    assert all(c["sha"] and c["short"] and c["author"] == "QA Fixture" for c in commits)
    assert not any(c["is_merge"] for c in commits)
    merge = repo.commits(git_repo.merge_sha, limit=1)
    assert merge[0]["is_merge"] is True
    assert len(repo.parents(git_repo.merge_sha)) == 2
    assert len(repo.parents(git_repo.base_sha)) == 0
    assert repo.commit_subject(git_repo.base_sha) == "base commit"
    assert repo.total_commits() >= 6


def test_ahead_and_behind_is_directional(git_repo):
    """`ahead` is what the head ref has that the base does not; `behind` is the other number."""
    repo = GitRepository(git_repo.root)
    assert repo.ahead_behind("main", "feature/payments") == (2, 0)
    assert repo.ahead_behind("feature/payments", "main") == (0, 2)
    assert repo.ahead_behind("main", "main") == (0, 0)


def test_unmerged_reads_conflict_entries_from_the_index(conflicted_repo):
    """The authoritative conflict signal is an unmerged index entry, not marker text."""
    repo = GitRepository(conflicted_repo.root)
    assert repo.unmerged() == ["cfg.yaml"]
    assert any(line.startswith("u ") for line in repo.status_porcelain())


def test_unmerged_is_empty_when_no_merge_is_in_progress(git_repo):
    """Merge commits in the history are not conflicts; only the index can say so."""
    assert GitRepository(git_repo.root).unmerged() == []


def test_unmerged_undoes_gits_path_quoting(conflicted_repo_utf8):
    """git quotes a non-ASCII path as octal bytes; a report must show the real name."""
    quoted = conflicted_repo_utf8.git("ls-files", "--unmerged").stdout
    assert '"caf\\303\\251.yaml"' in quoted, "the fixture must exercise the quoted form"
    assert GitRepository(conflicted_repo_utf8.root).unmerged() == ["café.yaml"]


def test_reading_a_conflict_leaves_the_index_alone(conflicted_repo):
    repo = GitRepository(conflicted_repo.root)
    before = conflicted_repo.state()
    repo.unmerged()
    repo.status_porcelain()
    assert conflicted_repo.state() == before


def test_commit_files_reads_each_commit_from_one_log(git_repo):
    repo = GitRepository(git_repo.root)
    listing = repo.commit_files("feature/payments", base=git_repo.base_sha)
    assert len(listing) == 5
    assert listing[git_repo.shas["add the scratch file"]] == [
        {"status": "A", "path": "gone.txt", "old_path": None}]
    assert listing[git_repo.shas["change target signature and rename legacy"]] == [
        {"status": "R100", "path": "current.txt", "old_path": "legacy.txt"},
        {"status": "M", "path": "src/app.py", "old_path": None}]


def test_commit_files_gives_a_merge_commit_no_invented_diff(git_repo):
    listing = GitRepository(git_repo.root).commit_files("main", base=git_repo.base_sha)
    assert listing[git_repo.merge_sha] == [], "a merge is not diffed against a guessed parent"


def test_operation_markers_name_the_paused_operation(git_repo, conflicted_repo, rebasing_repo):
    assert GitRepository(git_repo.root).operation_in_progress() is None
    assert GitRepository(conflicted_repo.root).operation_in_progress() == "merge"
    assert GitRepository(rebasing_repo.root).operation_in_progress() == "rebase"


def test_reading_the_operation_does_not_touch_the_repository(rebasing_repo):
    repo = GitRepository(rebasing_repo.root)
    before = rebasing_repo.state()
    repo.operation_in_progress()
    repo.status_porcelain()
    assert rebasing_repo.state() == before


@pytest.mark.parametrize("body,expected", [
    ("Signed-off-by: A <a@example.invalid>", {"signed-off-by": ["A <a@example.invalid>"]}),
    ("Subject prose\n\nReviewed-by: B\nAcked-by: C",
     {"reviewed-by": ["B"], "acked-by": ["C"]}),
    ("This fixes: the crash", {}),                      # a key cannot contain a space
    ("Signed-off-by: A\n\nthen more prose after it", {}),  # not the last paragraph
    ("", {}),
])
def test_trailers_are_only_the_last_paragraph_of_key_value_lines(body, expected):
    assert git_runner.parse_trailers(body) == expected
