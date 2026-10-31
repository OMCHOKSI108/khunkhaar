"""Read-only git intelligence (Phase 3, Module A).

`runner` can execute git, `model` describes what was found, and `analysis` turns one into the
other. Nothing here writes to a repository: the subcommand allowlist, the ref validation and the
before/after index snapshot all live in `runner`, and the other two modules never spawn a process.
"""

from __future__ import annotations

from core.git.model import ChangeSet, FileChange, RepoIdentity, WorktreeState
from core.git.runner import (GitError, GitRepository, RepositoryUntrustedError,
                             StateChangedError, open_repository, validate_path, validate_ref,
                             validate_range)

__all__ = ["ChangeSet", "FileChange", "GitError", "GitRepository", "RepoIdentity",
           "RepositoryUntrustedError", "StateChangedError", "WorktreeState", "open_repository",
           "validate_path", "validate_ref", "validate_range"]
