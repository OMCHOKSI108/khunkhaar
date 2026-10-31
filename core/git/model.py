"""What a git analysis contains.

These are plain data holders with no git knowledge of their own: `analysis` fills them from the
read-only runner, the CLI and the dashboard read them. Everything here is derived from git output
that already existed - a count of files is a count of files, never an inferred intention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUS_WORDS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_change",
    "U": "unmerged",
    "?": "untracked",
    "!": "ignored",
}


def status_word(letter: str) -> str:
    return STATUS_WORDS.get((letter or "")[:1].upper(), "modified")


@dataclass
class RepoIdentity:
    """Where the analysis happened, as git reports it."""

    root: str = ""
    toplevel: str = ""
    head: str = ""
    short_head: str = ""
    branch: str = ""
    detached: bool = False
    operation: str | None = None      # merge | rebase | cherry-pick | revert | bisect | none
    remote: str | None = None
    base_branch: str | None = None
    base_source: str = "none"          # explicit | candidate | none
    ahead: int = 0
    behind: int = 0
    upstream: str | None = None
    commit_count: int = 0
    worktree_clean: bool = True
    conflicted: bool = False
    reads: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class WorktreeState:
    """Staged, unstaged and untracked content, kept separate because they are three questions."""

    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    ignored_count: int = 0

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicted)

    @property
    def dirty(self) -> bool:
        return not self.clean

    def to_dict(self) -> dict[str, Any]:
        return {
            "staged": self.staged, "unstaged": self.unstaged, "untracked": self.untracked,
            "conflicted": self.conflicted, "ignored_count": self.ignored_count,
            "clean": self.clean,
            "counts": {
                "staged": len(self.staged), "unstaged": len(self.unstaged),
                "untracked": len(self.untracked), "conflicted": len(self.conflicted),
            },
        }


@dataclass
class FileChange:
    """One file in a change set.

    `attribution` says where `symbols` came from: `python-ast` means the two file versions were
    parsed and the names are exact, `git-hunk-header` means they are git's nearest-line guess, and
    `none` means nothing named them. git's raw label always survives in `hunk_contexts`, so nothing
    that was thrown away is missing from the report and nothing that is a guess is called a fact.
    """

    path: str
    old_path: str | None = None
    status: str = "modified"
    insertions: int = 0
    deletions: int = 0
    binary: bool = False
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False
    functions: list[str] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    attribution: str = "none"
    hunk_contexts: list[str] = field(default_factory=list)
    hunks: int = 0
    language: str | None = None

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        data["churn"] = self.churn
        return data


@dataclass
class ChangeSet:
    """The answer to `what changed`, in a shape the impact engine and the report can both use."""

    kind: str = "range"                 # range | commit | working-tree
    base: str | None = None
    head: str | None = None
    base_source: str = "none"
    commits: list[dict[str, Any]] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    conflicted: bool = False
    worktree_clean: bool = True
    index_untouched: bool = True
    notes: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def renames(self) -> list[FileChange]:
        return [f for f in self.files if f.status in ("renamed", "copied")]

    @property
    def removed(self) -> list[FileChange]:
        return [f for f in self.files if f.status == "deleted"]

    @property
    def added(self) -> list[FileChange]:
        return [f for f in self.files if f.status == "added"]

    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    def summary(self) -> dict[str, Any]:
        return {
            "commits": len(self.commits),
            "files_changed": len(self.files),
            "insertions": self.insertions,
            "deletions": self.deletions,
            "renames": len(self.renames),
            "removed": len(self.removed),
            "added": len(self.added),
            "conflicted": self.conflicted,
            "worktree_clean": self.worktree_clean,
            "index_untouched": self.index_untouched,
            "merge_commits": sum(1 for c in self.commits if c.get("is_merge")),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "base": self.base, "head": self.head,
            "base_source": self.base_source, "summary": self.summary(),
            "commits": self.commits, "files": [f.to_dict() for f in self.files],
            "conflicted": self.conflicted, "worktree_clean": self.worktree_clean,
            "index_untouched": self.index_untouched, "truncated": self.truncated,
            "notes": self.notes,
        }
