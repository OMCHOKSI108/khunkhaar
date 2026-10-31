"""Module A: read a repository's state and its changes without touching either.

The analysis is built from git's own machine-readable output - `status --porcelain=v2`,
`diff --name-status -M`, `diff --numstat` and `diff --unified=0` - so staged, unstaged and
untracked content stay three separate facts instead of being collapsed into "dirty". Symbol
attribution is labelled with where it came from: Python files are parsed and their changed
definitions named exactly (`python-ast`), everything else keeps git's hunk-header label as the
heuristic it is (`git-hunk-header`), and a file nothing named is `none` rather than an empty list.
The tree-sitter pass in Module C extends the exact branch to the other languages it has grammars for.

Conflict state is read from unmerged index entries, not from `<<<<<<<` text in a file, because a
file can contain those characters without a merge being in progress and a conflicted file is not
guaranteed to be readable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.git.model import ChangeSet, FileChange, RepoIdentity, WorktreeState, status_word
from core.git.runner import (GitError, GitRepository, unquote, validate_range,
                             validate_ref)
from core.git.symbols import NotAttributed, attribute as attribute_symbols
from core.shared.config import current

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: (?P<context>.*))?$")
_HUNK_RANGE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_GIT_FILE = re.compile(r"^diff --git a/(?P<old>.*) b/(?P<new>.+)$")

# One changed line per integer in a PatchDetail. A million-line generated file is not worth the
# memory, and attribution that ran out says so instead of quietly listing fewer symbols.
_MAX_TRACKED_LINES = 5_000

# Extensions for which git itself computes an enclosing-declaration regex for the hunk header
# (its builtin `userdiff` drivers). For anything else git prints the preceding line, which is
# text, not a symbol - so it is reported as `hunk_context` and never as a function.
GIT_FUNCNAME_SUFFIXES = frozenset({
    ".py", ".pyi", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm",
    ".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".php", ".rb", ".sh",
    ".bash", ".ksh", ".zsh", ".css", ".html", ".htm", ".tex", ".sty", ".cls", ".for", ".f",
    ".f90", ".f77", ".ads", ".adb", ".v", ".sv", ".vhdl", ".vhd", ".ps1", ".cs", ".as",
})


def has_declaration_driver(path: str) -> bool:
    return Path(path).suffix.lower() in GIT_FUNCNAME_SUFFIXES


LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rb": "ruby",
    ".java": "java", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".rs": "rust", ".php": "php", ".cs": "csharp", ".kt": "kotlin", ".swift": "swift",
    ".scala": "scala", ".sh": "shell", ".sql": "sql", ".html": "html", ".css": "css",
    ".scss": "scss", ".vue": "vue", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".md": "markdown",
}


def language_for(path: str) -> str | None:
    return LANGUAGES.get(Path(path).suffix.lower())


def _record_paths(tail: str) -> tuple[str, str | None]:
    """A porcelain v2 rename record carries `new_path<TAB>old_path`."""
    new, _, old = tail.partition("\t")
    return unquote(new.strip()), (unquote(old.strip()) if old.strip() else None)


# ------------------------------------------------------------------ status
def parse_status(lines: list[str]) -> WorktreeState:
    """Staged, unstaged, untracked and conflicted paths, each kept separate."""
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    conflicted: list[str] = []
    ignored = 0
    for line in lines:
        if line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        if kind == "?":
            untracked.append(unquote(rest.strip()))
            continue
        if kind == "!":
            ignored += 1
            continue
        if kind == "u":
            # u <XY> <sub> <mode x4> <sha x3> <path>: eleven fields, path last
            parts = line.split(maxsplit=10)
            if len(parts) == 11:
                conflicted.append(unquote(parts[10]))
            continue
        if kind not in ("1", "2"):
            continue
        # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
        # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <R><score> <path><TAB><origPath>
        count = 10 if kind == "2" else 9
        parts = line.split(maxsplit=count - 1)
        if len(parts) < count:
            continue
        index_code, worktree_code = parts[1][0], (parts[1][1] if len(parts[1]) > 1 else ".")
        if kind == "2":
            new, old = _record_paths(parts[9])
            paths = [new] + ([old] if old and index_code == "D" else [])
        else:
            paths = [unquote(parts[8])]
        if index_code not in ".N":
            staged.extend(paths)
        if worktree_code not in ".N":
            unstaged.extend(paths)
    return WorktreeState(staged=_ordered(staged), unstaged=_ordered(unstaged),
                         untracked=_ordered(untracked), conflicted=_ordered(conflicted),
                         ignored_count=ignored)


def _ordered(items: list[str]) -> list[str]:
    return sorted(dict.fromkeys(items))


def branch_facts(lines: list[str]) -> dict[str, str]:
    """The `# branch.*` header lines of porcelain v2, which already know HEAD and upstream."""
    facts: dict[str, str] = {}
    for line in lines:
        if not line.startswith("# branch."):
            continue
        _, _, remainder = line.partition("# branch.")
        key, _, value = remainder.strip().partition(" ")
        facts.setdefault(key, value)
    return facts


# ------------------------------------------------------------------ diff readers
def parse_name_status(lines: list[str]) -> dict[str, tuple[str, str | None]]:
    """`{new_path: (status letter, old path)}` from `diff --name-status -M`."""
    out: dict[str, tuple[str, str | None]] = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        letter = parts[0].strip()[:1].upper()
        if letter in ("R", "C") and len(parts) >= 3:
            out[unquote(parts[2])] = (letter, unquote(parts[1]))
        else:
            out[unquote(parts[-1])] = (letter, None)
    return out


def parse_numstat(lines: list[str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[-1]
        path = _rename_target(path)
        binary = added == "-" or removed == "-"
        numbers = (0, 0) if binary else (int(added or 0), int(removed or 0))
        out[unquote(path)] = numbers
    return out


def _rename_target(text: str) -> str:
    """numstat writes renames as `old => new`, sometimes `{a => b}` inside a shared directory."""
    cleaned = text.replace("{", "").replace("}", "").strip()
    if " => " in cleaned:
        _, _, tail = cleaned.rpartition(" => ")
        return tail.strip("/") or cleaned
    return cleaned


@dataclass
class PatchDetail:
    """What a unified diff says about one file: hunks, git's hunk-header labels, size, lines.

    `old_lines`/`new_lines` are the line numbers the diff removed and added, so a symbol can be
    attributed to the definitions those lines actually fall inside rather than to whatever text
    git printed above the hunk.
    """

    contexts: list[str] = field(default_factory=list)
    hunks: int = 0
    binary: bool = False
    added: int = 0
    removed: int = 0
    status_hint: str | None = None
    old_path: str | None = None
    truncated: bool = False
    old_lines: list[int] = field(default_factory=list)
    new_lines: list[int] = field(default_factory=list)
    lines_capped: bool = False


_DIFF_NOISE = ("index ", "--- ", "+++ ", "similarity index", "new mode ", "old mode ",
               "Subproject ")


def parse_patch(patch: str, *, limit_per_file: int = 200_000) -> dict[str, PatchDetail]:
    """Per-file hunk counts, enclosing-symbol labels, changed line numbers and binary flags."""
    files: dict[str, PatchDetail] = {}
    current: PatchDetail | None = None
    bytes_seen = 0
    old_no = new_no = 0
    for line in patch.splitlines():
        header = _GIT_FILE.match(line)
        if header:
            current = files.setdefault(unquote(header.group("new")), PatchDetail())
            bytes_seen = 0
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status_hint = "added"
        elif line.startswith("deleted file mode"):
            current.status_hint = "deleted"
        elif line.startswith(("Binary files", "GIT binary patch")):
            current.binary = True
        elif line.startswith(("rename from ", "copy from ")):
            current.old_path = unquote(line.split(" ", 1)[1])
        elif line.startswith("diff --git") or any(line.startswith(n) for n in _DIFF_NOISE):
            continue
        elif line.startswith("@@"):
            match = _HUNK.match(line)
            if not match:
                continue
            current.hunks += 1
            context = (match.group("context") or "").strip()
            if _is_symbol(context) and context not in current.contexts:
                current.contexts.append(context)
            bounds = _HUNK_RANGE.match(line)
            if bounds:
                old_no, new_no = int(bounds.group(1)), int(bounds.group(2))
        elif line.startswith("+"):
            current.added += 1
            _track(current.new_lines, new_no, current)
            new_no += 1
            bytes_seen = _count(current, bytes_seen, line, limit_per_file)
        elif line.startswith("-"):
            current.removed += 1
            _track(current.old_lines, old_no, current)
            old_no += 1
            bytes_seen = _count(current, bytes_seen, line, limit_per_file)
        elif line.startswith(" "):
            old_no += 1
            new_no += 1
    return files


def _track(target: list[int], number: int, detail: PatchDetail) -> None:
    if len(target) >= _MAX_TRACKED_LINES:
        detail.lines_capped = True
        return
    target.append(number)


def _count(detail: PatchDetail, seen: int, line: str, limit: int) -> int:
    total = seen + len(line)
    if total > limit:
        detail.truncated = True
    return total


def _is_symbol(context: str) -> bool:
    """git's xdiff heuristic prints the enclosing line; treat an empty one as 'unknown'."""
    text = context.strip()
    if text in ("", "{}"):
        return False
    return not text.startswith(("+++", "---", "@@"))


# ------------------------------------------------------------------ assembly
def _limits() -> tuple[int, int]:
    settings = current()
    return (int(settings.get("git", "max_files_in_report", 2000) or 2000),
            int(settings.get("git", "max_diff_bytes_per_file", 200000) or 200000))


def _files_from(names: dict[str, tuple[str, str | None]],
                counts: dict[str, tuple[int, int]],
                details: dict[str, PatchDetail]) -> tuple[list[FileChange], bool]:
    max_files, _ = _limits()
    paths = sorted(set(names) | set(counts) | set(details))
    truncated = len(paths) > max_files
    out: list[FileChange] = []
    for path in paths[:max_files]:
        letter, old = names.get(path, ("M", None))
        detail = details.get(path)
        if detail and detail.status_hint and path not in names:
            letter = {"added": "A", "deleted": "D"}.get(detail.status_hint, "M")
        insertions, deletions = counts.get(path, (0, 0))
        if not insertions and not deletions and detail:
            insertions, deletions = detail.added, detail.removed
        contexts = list(detail.contexts) if detail else []
        declared = contexts if has_declaration_driver(path) else []
        out.append(FileChange(
            path=path, old_path=old or (detail.old_path if detail else None),
            status=status_word(letter), insertions=insertions, deletions=deletions,
            binary=bool(detail.binary) if detail else False,
            hunks=detail.hunks if detail else 0,
            functions=declared, hunk_contexts=contexts,
            attribution="git-hunk-header" if declared else "none",
            language=language_for(path),
        ))
    return out, truncated


def _text(repo: GitRepository, rev: str, path: str, *, from_worktree: bool,
          limit: int) -> str | None:
    """One side of a diff as text, or None when it cannot be read."""
    if from_worktree:
        target = (repo.root / path).resolve()
        if not target.is_file() or repo.root not in target.parents:
            return None
        try:
            return target.read_bytes()[:limit].decode("utf-8", "replace")
        except OSError:
            return None
    try:
        # `blob()` returns "" rather than raising when the path does not exist at that rev,
        # which is the answer for an added or deleted file.
        return repo.blob(f"{rev}:{path}", limit=limit) or None
    except GitError:
        return None


def _attribute(repo: GitRepository, files: list[FileChange], details: dict[str, PatchDetail],
               *, old_rev: str | None, new_rev: str, new_from_worktree: bool,
               notes: list[str]) -> None:
    """Parse both sides of every changed Python file and name the definitions inside the hunks.

    This replaces git's hunk-header label for those files. Each one costs two blob reads, and every
    blob read is bracketed by the untouched-repository guard, so the pass is bounded by
    `git.max_symbol_attribution_files`; a file over that bound, unreadable or unparseable is
    reported as not attributed instead of being quietly credited to a guess.
    """
    settings = current()
    if not settings.get("git", "ast_symbol_attribution", True):
        return
    budget = settings.get("git", "max_symbol_attribution_files", 60)
    budget = 60 if budget is None else int(budget)
    limit = int(settings.get("impact", "max_source_bytes", 2_000_000) or 2_000_000)
    used = over_budget = 0
    unattributed: list[str] = []
    for change in files:
        if change.language != "python" or change.binary:
            continue
        detail = details.get(change.path)
        if detail is None and not change.untracked:
            continue
        if used >= budget:
            over_budget += 1
            continue
        used += 1
        new_file = change.untracked or change.status == "added" or old_rev is None
        deleted = change.status == "removed"
        old_path = change.old_path or change.path
        old_text = (_text(repo, old_rev, old_path, from_worktree=False, limit=limit)
                    if deleted or not new_file else None)
        new_text = (_text(repo, new_rev, change.path, from_worktree=new_from_worktree,
                          limit=limit) if not deleted else None)
        try:
            found = attribute_symbols(
                old_text, new_text,
                list(detail.old_lines) if detail else [],
                list(detail.new_lines) if detail else [],
                new_file=new_file, deleted=deleted)
        except NotAttributed:
            unattributed.append(change.path)
            continue
        change.symbols = found
        change.attribution = "python-ast"
        change.functions = [item["name"] for item in found]
        if detail is not None and detail.lines_capped:
            notes.append(f"{change.path}: more changed lines than the {_MAX_TRACKED_LINES}-line "
                         f"attribution window allows, so symbols near the end of the diff were "
                         f"not attributed")
    if over_budget:
        notes.append(f"{over_budget} Python file(s) over git.max_symbol_attribution_files were "
                     f"left attributed by git's hunk header, not parsed")
    if unattributed:
        notes.append(
            f"{len(unattributed)} .py file(s) could not be attributed from their content "
            f"({', '.join(unattributed[:3])}{'...' if len(unattributed) > 3 else ''}): either they "
            f"do not parse as Python 3 or one side of the diff could not be read; their symbols "
            f"are unknown, not absent")


def _attach_commit_files(repo: GitRepository, set_: ChangeSet, *, base: str, head: str,
                         notes: list[str]) -> None:
    """Per-commit file lists, from one extra `git log --name-status` read.

    This is bounded per commit rather than per analysis: a branch that touches 300 files in one
    commit should still list its other commits' files, so each entry is capped on its own and the
    cap is reported in the entry instead of silently shortening a commit.
    """
    settings = current()
    if not set_.commits or not settings.get("git", "commit_file_lists", True):
        return
    per_commit = settings.get("git", "max_files_per_commit", 40)
    per_commit = 40 if per_commit is None else int(per_commit)
    listing = repo.commit_files(head, base=base)
    if not listing:
        notes.append("per-commit file lists could not be read from `git log --name-status`: the "
                     "change set above is unaffected, the commit list carries no file detail")
        return
    merges_without_files = 0
    for commit in set_.commits:
        entries = listing.get(commit["sha"], [])
        commit["files_changed"] = len(entries)
        commit["files"] = entries[:per_commit]
        commit["files_truncated"] = len(entries) > per_commit
        if not entries and commit.get("is_merge"):
            merges_without_files += 1
    if merges_without_files:
        notes.append(f"{merges_without_files} merge commit(s) carry no file list: git does not "
                     f"diff a merge against a parent unless one is chosen, and choosing one would "
                     f"be a different claim than the one that was asked for")


def _change_set(repo: GitRepository, source: str, target: str, *, kind: str, base_label: str,
                head_label: str, base_source: str, notes: list[str]) -> ChangeSet:
    limit = _limits()[1]
    names = parse_name_status(repo.diff_name_status(source, target))
    counts = parse_numstat(repo.diff_numstat(source, target))
    details = parse_patch(repo.diff_patch(source, target, context=0), limit_per_file=limit)
    files, truncated = _files_from(names, counts, details)
    _attribute(repo, files, details, old_rev=source, new_rev=target, new_from_worktree=False,
               notes=notes)
    state = parse_status(repo.status_porcelain())
    set_ = ChangeSet(kind=kind, base=base_label, head=head_label, base_source=base_source,
                     commits=repo.commits(target, base=source), files=files,
                     conflicted=bool(state.conflicted), worktree_clean=state.clean,
                     index_untouched=True, notes=notes, truncated=truncated)
    _attach_commit_files(repo, set_, base=source, head=target, notes=notes)
    _note_truncation(set_)
    return set_


def _note_truncation(set_: ChangeSet) -> None:
    if any(f.binary for f in set_.files):
        set_.notes.append(f"{sum(1 for f in set_.files if f.binary)} binary file(s): git "
                          f"reports them as changed without line counts")
    guessed = sorted({f.path for f in set_.files if f.attribution == "git-hunk-header"})
    if guessed:
        set_.notes.append(
            f"{len(guessed)} file(s) are attributed with git's hunk-header label "
            f"({', '.join(guessed[:3])}{'...' if len(guessed) > 3 else ''}): that is the nearest "
            f"line git could find above the hunk, a heuristic and not a parsed symbol")
    silent = sorted({f.path for f in set_.files if f.hunks and f.attribution == "none"})
    if silent:
        set_.notes.append(
            f"{len(silent)} file(s) have hunks no definition was named for "
            f"({', '.join(silent[:3])}{'...' if len(silent) > 3 else ''}): either the file type "
            f"has no parser here or the change lies outside any definition; unknown, not absent")
    if set_.truncated:
        set_.notes.append("file list truncated to the configured git.max_files_in_report")


def _commit_change_set(repo: GitRepository, sha: str, notes: list[str]) -> ChangeSet:
    """One commit.

    `git show` on a merge prints a combined diff that omits every file matching any parent, which
    is not the same claim as 'the change against the first parent' - so a commit with parents is
    read as an explicit two-tree diff and only a root commit, which has no parent to name, goes
    through `git show`.
    """
    limit = _limits()[1]
    parents = repo.parents(sha)
    if parents:
        if len(parents) > 1:
            notes.append(f"merge commit {sha[:10]}: this is the change against its first parent "
                         f"{parents[0][:10]}; the second parent's content is not attributed here")
        names = parse_name_status(repo.diff_name_status(parents[0], sha))
        counts = parse_numstat(repo.diff_numstat(parents[0], sha))
        details = parse_patch(repo.diff_patch(parents[0], sha, context=0), limit_per_file=limit)
        base_label = f"{sha}^"
        attribution_old = parents[0]
    else:
        notes.append(f"root commit {sha[:10]}: every file is an addition, compared with "
                     f"`git show` because there is no parent to diff against")
        names = parse_name_status(repo.show_name_status(sha))
        counts = parse_numstat(repo.show_numstat(sha))
        details = parse_patch(repo.show_patch(sha, context=0), limit_per_file=limit)
        base_label = "(no parent)"
        attribution_old = None
    files, truncated = _files_from(names, counts, details)
    _attribute(repo, files, details, old_rev=attribution_old, new_rev=sha,
               new_from_worktree=False, notes=notes)
    state = parse_status(repo.status_porcelain())
    set_ = ChangeSet(kind="commit", base=base_label, head=sha, base_source="parent",
                     commits=repo.commits(sha, limit=1), files=files,
                     conflicted=bool(state.conflicted), worktree_clean=state.clean,
                     index_untouched=True, notes=notes, truncated=truncated)
    _note_truncation(set_)
    return set_


def _working_tree_change_set(repo: GitRepository, notes: list[str]) -> ChangeSet:
    notes.append("index and working tree against HEAD: staged, unstaged and untracked files stay "
                 "separate, and nothing was added to the index to produce this view")
    limit = _limits()[1]
    names = parse_name_status(repo.diff_name_status("HEAD"))
    counts = parse_numstat(repo.diff_numstat("HEAD"))
    details = parse_patch(repo.diff_patch("HEAD", context=0), limit_per_file=limit)
    files, truncated = _files_from(names, counts, details)
    state = parse_status(repo.status_porcelain())
    staged, unstaged, untracked = (set(state.staged), set(state.unstaged), set(state.untracked))
    for path in untracked:
        if path not in {f.path for f in files}:
            files.append(FileChange(path=path, status="untracked", untracked=True,
                                    language=language_for(path)))
    for change in files:
        change.staged = change.path in staged
        change.unstaged = change.path in unstaged
        change.untracked = change.path in untracked or change.status == "untracked"
    _attribute(repo, files, details, old_rev="HEAD", new_rev="HEAD", new_from_worktree=True,
               notes=notes)
    files.sort(key=lambda change: change.path)
    set_ = ChangeSet(kind="working-tree", base="HEAD", head="(working tree)",
                     base_source="head", commits=[], files=files,
                     conflicted=bool(state.conflicted), worktree_clean=state.clean,
                     index_untouched=True, notes=notes, truncated=truncated)
    _note_truncation(set_)
    return set_


# ------------------------------------------------------------------ public reads
def identity(repo: GitRepository, *, base: str | None = None) -> RepoIdentity:
    """Repository identity from local refs only. No remote is ever contacted."""
    lines = repo.status_porcelain()
    facts = branch_facts(lines)
    state = parse_status(lines)
    base_ref, how = repo.resolve_base(base)
    branch = facts.get("head", "") or repo.current_branch()
    detached = branch in ("(detached)", "(branch: detached)") or repo.is_detached()
    head_sha = facts.get("oid") or repo.head()
    ahead, behind = repo.ahead_behind("HEAD", base_ref) if base_ref else (0, 0)
    return RepoIdentity(
        root=str(repo.root), toplevel=repo.toplevel(), head=head_sha,
        short_head=head_sha[:10], branch=branch, detached=detached,
        operation=repo.operation_in_progress(), remote=repo.remote_url(),
        base_branch=base_ref, base_source=how, ahead=ahead, behind=behind,
        upstream=facts.get("upstream"), commit_count=repo.total_commits(),
        worktree_clean=state.clean, conflicted=bool(state.conflicted), reads=repo.reads,
    )


def worktree(repo: GitRepository) -> WorktreeState:
    return parse_status(repo.status_porcelain())


def analyze(repo: GitRepository, *, base: str | None = None, head: str | None = None,
            commit: str | None = None, working_tree: bool = False, branch: str | None = None,
            range_spec: str | None = None, mr: bool = False) -> ChangeSet:
    """Resolve the requested comparison and read it.

    Modes are mutually exclusive and checked in this order, so `--commit` cannot silently be
    widened by a stray `--base`, and the report always says which comparison was actually made.
    `mr=True` is a merge-request comparison: it always reads from the merge base, because a
    target branch that moved after the source branched is not part of the merge request.
    """
    notes: list[str] = []
    if mr:
        if commit or working_tree or branch or range_spec:
            raise GitError("a merge-request comparison cannot be combined with --commit, "
                           "--working-tree, --branch or --range")
        source = validate_ref(head) if head else "HEAD"
        if not repo.verify_ref(source):
            raise GitError(f"merge-request source ref {source!r} does not resolve locally; "
                           f"KHUNKHAAR never fetches, so fetch it yourself if it is a remote "
                           f"branch")
        if base:
            target = validate_ref(base)
            if not repo.verify_ref(target):
                raise GitError(f"merge-request target ref {target!r} does not resolve locally; "
                               f"KHUNKHAAR never fetches, so fetch it yourself if it is a remote "
                               f"branch")
            how = "merge-request"
        else:
            target, how = repo.resolve_base(None)
            if not target:
                raise GitError("no merge-request target: none of the configured base branches "
                               "exist locally and --base/--mr-target was not given")
        merge_base = repo.merge_base(target, source)
        if not merge_base:
            raise GitError(f"merge request source {source} and target {target} share no history, "
                           f"so there is no merge base to compare from")
        notes.append(f"merge-request comparison from the merge base {merge_base[:10]}: what "
                     f"{source} added since it diverged from {target}, not the whole tree "
                     f"difference")
        return _change_set(repo, merge_base, source, kind="merge-request", base_label=target,
                           head_label=source, base_source=how, notes=notes)
    if commit:
        sha = repo.verify_ref(validate_ref(commit))
        if not sha:
            raise GitError(f"commit {commit!r} does not resolve in this repository")
        return _commit_change_set(repo, sha, notes)
    if working_tree:
        return _working_tree_change_set(repo, notes)
    if range_spec:
        from_ref, to_ref = validate_range(range_spec)
        if not repo.verify_ref(from_ref) or not repo.verify_ref(to_ref):
            raise GitError(f"range {range_spec!r} does not resolve to two local commits")
        source = from_ref
        if "..." in range_spec:
            merge_base = repo.merge_base(from_ref, to_ref)
            if not merge_base:
                raise GitError(f"{from_ref} and {to_ref} share no history, so the three-dot "
                               f"range has no merge base")
            source = merge_base
            notes.append(f"three-dot range: compared from the merge base {merge_base[:10]}, so "
                         f"only what {to_ref} added since the two refs diverged is listed")
        else:
            notes.append("two-dot comparison: the whole tree difference between the two commits, "
                         "not only what one side added")
        return _change_set(repo, source, to_ref, kind="range", base_label=from_ref,
                           head_label=to_ref, base_source="explicit", notes=notes)
    head_label = validate_ref(head) if head else "HEAD"
    if not repo.verify_ref(head_label):
        raise GitError(f"head ref {head_label!r} does not resolve in this repository")
    if branch:
        return _branch_change_set(repo, validate_ref(branch), base, notes)
    base_ref, how = repo.resolve_base(base)
    if not base_ref:
        raise GitError("no comparison base: none of the configured base branches exist locally "
                       "and --base was not given")
    merge_base = repo.merge_base(base_ref, head_label)
    source = merge_base or base_ref
    if merge_base and merge_base != repo.verify_ref(base_ref):
        notes.append(f"compared from the merge base {merge_base[:10]}, not the tip of "
                     f"{base_ref}, because {base_ref} moved after the branches diverged")
    else:
        notes.append(f"compared {base_ref} directly against {head_label}")
    return _change_set(repo, source, head_label, kind="range", base_label=base_ref,
                       head_label=head_label, base_source=how, notes=notes)


def _branch_change_set(repo: GitRepository, branch: str, base: str | None,
                       notes: list[str]) -> ChangeSet:
    if not repo.verify_ref(branch):
        raise GitError(f"branch {branch!r} does not exist locally - KHUNKHAAR never fetches")
    base_ref, how = repo.resolve_base(base)
    if not base_ref:
        raise GitError("no comparison base for --branch: pass --base explicitly")
    merge_base = repo.merge_base(base_ref, branch)
    if not merge_base:
        raise GitError(f"{branch} and {base_ref} share no history; use --range to compare them "
                       f"as trees")
    notes.append(f"changes {branch} introduced since it diverged from {base_ref} "
                 f"(merge base {merge_base[:10]})")
    return _change_set(repo, merge_base, branch, kind="branch", base_label=base_ref,
                       head_label=branch, base_source=how, notes=notes)
