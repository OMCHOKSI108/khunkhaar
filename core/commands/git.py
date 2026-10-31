"""`khunkhaar git status|analyze` - Module A, read-only repository intelligence.

Both commands refuse a repository that is not registered: KHUNKHAAR may read a company checkout
once the operator has added it on purpose, and never because a directory happened to exist.
Neither command can write to the repository; every read is bracketed by a git status/HEAD
snapshot, so a repository that moved during the analysis aborts it instead of producing a report
about a state that no longer exists.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.git import analysis, model as git_model
from core.git.runner import GitError, RepositoryUntrustedError, StateChangedError, open_repository
from core.metadata import store
from core.project_discovery import detector
from core.shared import evidence, paths
from core.shared.config import current


def _resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _project_id(root: Path) -> str | None:
    """The registry row for this repository, or None when it was never registered."""
    project_id = detector.project_id_for(root)
    return project_id if store.get_project(project_id) else None


def _require_registered(root: Path, *, enforce: bool) -> tuple[str | None, str | None]:
    """(project_id, error). Registration is analysis permission; testing still needs a grant."""
    project_id = _project_id(root)
    if project_id or not enforce:
        return project_id, None
    return None, (f"{root} is not a registered project. Run "
                  f"`khunkhaar project register --path {root}` first - KHUNKHAAR does not analyse "
                  f"repositories that were never added on purpose.")


def _analyze_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="khunkhaar git analyze")
    parser.add_argument("--path", default=".", help="repository to analyse")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--base", metavar="REF", help="comparison base (default: from config)")
    group.add_argument("--commit", metavar="REF", help="analyse one commit against its parent")
    group.add_argument("--range", dest="range_spec", metavar="A..B",
                       help="compare two refs as trees, exactly as git does")
    group.add_argument("--working-tree", action="store_true",
                       help="index and working tree against HEAD")
    group.add_argument("--branch", metavar="REF",
                       help="what a local branch added since it diverged from the base")
    parser.add_argument("--head", metavar="REF", help="head ref to compare (default: HEAD)")
    parser.add_argument("--mr-source", metavar="REF",
                        help="merge-request source branch (alias for --head; merge-base "
                             "comparison)")
    parser.add_argument("--mr-target", metavar="REF",
                        help="merge-request target branch (alias for --base; default: configured "
                             "base)")
    parser.add_argument("--limit", type=int, default=40, help="files listed in the terminal")
    parser.add_argument("--save", dest="save", action="store_true", default=True,
                        help="write the artifact and the git_analyses row (default)")
    parser.add_argument("--no-save", dest="save", action="store_false",
                        help="print only; nothing is written anywhere")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar git status")
    parser.add_argument("--path", default=".", help="repository to read")
    parser.add_argument("--base", metavar="REF", help="comparison base for ahead/behind")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    root = _resolve_path(args.path)
    project_id, refusal = _require_registered(root, enforce=bool(
        current().get("git", "require_registered_project", True)))
    if refusal:
        print(f"khunkhaar git status: {refusal}")
        return 2
    try:
        repo = open_repository(root)
        info = analysis.identity(repo, base=args.base)
        state = analysis.worktree(repo)
    except (RepositoryUntrustedError, GitError) as exc:
        code = 2 if isinstance(exc, RepositoryUntrustedError) else 1
        print(f"khunkhaar git status: {exc}")
        return code
    except StateChangedError as exc:
        print(f"khunkhaar git status: {exc}")
        return 3

    payload = {"project_id": project_id, "repository": info.to_dict(),
               "worktree": state.to_dict(), "reads": repo.reads,
               "index_untouched": True,
               "note": "read-only: git status is reported as git itself reports it, with "
                       "--no-optional-locks so the index was not refreshed"}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    _print_identity(info)
    _print_worktree(state)
    print(f"{'git reads':<14}{repo.reads} subprocess call(s), all read-only")
    return 0


def analyze(argv: list[str]) -> int:
    args = _analyze_args(argv)
    mr = bool(args.mr_source or args.mr_target)
    if mr:
        conflicting = [flag for flag, value in (
            ("--base", args.base), ("--head", args.head), ("--commit", args.commit),
            ("--range", args.range_spec), ("--working-tree", args.working_tree),
            ("--branch", args.branch)) if value]
        if conflicting:
            print(f"khunkhaar git analyze: --mr-source/--mr-target cannot be combined with "
                  f"{', '.join(conflicting)}")
            return 2
        if not args.mr_source:
            print("khunkhaar git analyze: --mr-target requires --mr-source "
                  "(the branch that carries the merge request's changes)")
            return 2
        args.head = args.mr_source
        args.base = args.mr_target or None
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_registered(root, enforce=bool(
        current().get("git", "require_registered_project", True)))
    if refusal:
        print(f"khunkhaar git analyze: {refusal}")
        return 2
    try:
        repo = open_repository(root)
        changeset = analysis.analyze(repo, base=args.base, head=args.head, commit=args.commit,
                                     range_spec=args.range_spec, working_tree=args.working_tree,
                                     branch=args.branch, mr=mr)
        info = analysis.identity(repo, base=args.base)
    except (RepositoryUntrustedError, GitError) as exc:
        code = 2 if isinstance(exc, RepositoryUntrustedError) else 1
        print(f"khunkhaar git analyze: {exc}")
        return code
    except StateChangedError as exc:
        print(f"khunkhaar git analyze: {exc}")
        return 3

    payload = {"project_id": project_id, "repository": info.to_dict(),
               "changeset": changeset.to_dict(), "reads": repo.reads,
               "requested": {"base": args.base, "head": args.head, "commit": args.commit,
                             "range": args.range_spec, "working_tree": args.working_tree,
                             "branch": args.branch, "mr_source": args.mr_source,
                             "mr_target": args.mr_target},
               "index_untouched": changeset.index_untouched}
    artifact = None
    if args.save and project_id:
        artifact = _persist(project_id, payload, changeset)
        payload["artifact"] = artifact
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"repository      {info.toplevel}")
    print(f"branch          {info.branch} at {info.short_head}"
          + (f"  ({info.ahead} ahead, {info.behind} behind {info.base_branch})"
             if info.base_branch else ""))
    print(f"comparison      {changeset.kind}: "
          f"{changeset.base or '-'} -> {changeset.head or '-'}"
          f"  [base chosen by {changeset.base_source}]")
    summary = changeset.summary()
    print(f"scale           {summary['commits']} commit(s), "
          f"{summary['files_changed']} file(s), +{summary['insertions']} "
          f"-{summary['deletions']}, {summary['renames']} rename(s), "
          f"{summary['removed']} removed, {summary['added']} added")
    if summary["merge_commits"]:
        print(f"merge commits   {summary['merge_commits']}")
    if changeset.conflicted:
        print("conflict        the repository is mid-merge; the change set is unreliable until "
              "it is resolved")
    if not changeset.files:
        print("files           none")
    for change in changeset.files[:max(0, args.limit)]:
        flags = _flag(change)
        label = f"{change.path}" + (f"  (from {change.old_path})" if change.old_path else "")
        print(f"  {change.status:<9}{flags}{change.insertions:>5}{change.deletions:>6}"
              f"  {label}")
        for symbol in change.symbols[:4]:
            print(f"  {'':<9}  {symbol['change']:<9}{symbol['kind']:<15}"
                  f"{symbol['name']}  [{change.attribution}]")
        if not change.symbols and change.attribution == "git-hunk-header":
            for context in change.functions[:4]:
                print(f"  {'':<9}  near {context!r}  [git hunk header, a heuristic]")
    attributed: dict[str, int] = {}
    for change in changeset.files:
        attributed[change.attribution] = attributed.get(change.attribution, 0) + 1
    if attributed:
        print("attribution     " + "  ".join(
            f"{name}={count}" for name, count in sorted(attributed.items())))
    if len(changeset.files) > args.limit:
        print(f"  ... {len(changeset.files) - args.limit} more (full list in the artifact)")
    for note in changeset.notes:
        print(f"note            {note}")
    print(f"{'git reads':<14}{repo.reads} subprocess call(s), all read-only; index and worktree "
          f"verified unchanged")
    if artifact:
        print(f"artifact        {artifact}")
    return 0


def _flag(change: git_model.FileChange) -> str:
    """`SWUB` - staged, working-tree, untracked, binary. A readable one-token state."""
    return "".join([
        "S" if change.staged else ".",
        "W" if change.unstaged else ".",
        "U" if change.untracked else ".",
        "B" if change.binary else ".",
    ]) + "  "


def _print_identity(info: git_model.RepoIdentity) -> None:
    rows = [
        ("toplevel", info.toplevel),
        ("branch", info.branch + (" (detached)" if info.detached else "")),
        ("operation", info.operation or "-"),
        ("head", info.short_head),
        ("base", f"{info.base_branch or '-'} [by {info.base_source}]"),
        ("ahead/behind", f"{info.ahead}/{info.behind}" if info.base_branch else "-"),
        ("upstream", info.upstream or "-"),
        ("remote", info.remote or "-"),
        ("commits", info.commit_count),
    ]
    for label, value in rows:
        print(f"{label:<14}{value}")


def _print_worktree(state: git_model.WorktreeState) -> None:
    counts = state.to_dict()["counts"]
    print(f"{'worktree':<14}{'clean' if state.clean else 'dirty'}  "
          f"staged={counts['staged']} unstaged={counts['unstaged']} "
          f"untracked={counts['untracked']} conflicted={counts['conflicted']}")
    for label, items in (("staged", state.staged), ("unstaged", state.unstaged),
                         ("untracked", state.untracked), ("conflicted", state.conflicted)):
        for item in items[:10]:
            print(f"  {label:<10}{item}")
        if len(items) > 10:
            print(f"  {label:<10}... {len(items) - 10} more")


def _persist(project_id: str, payload: dict, changeset: git_model.ChangeSet) -> str:
    """Store the analysis as a 0600 evidence artifact plus a metadata row of counts only."""
    run_id = evidence.new_run_id("git")
    directory = paths.evidence_dir(project_id, "git-analysis")
    name = f"{run_id}-{changeset.kind}-{evidence.slugify(str(changeset.head))}.json"
    target = evidence.write_json(directory, name, payload, project_id=project_id, run_id=run_id,
                                kind="git-analysis")
    try:
        store.record_git_analysis(project_id, run_id, changeset.kind, base_ref=changeset.base,
                                  head_ref=changeset.head, summary=changeset.summary(),
                                  artifact=evidence.relative(target))
    except Exception as exc:  # the artifact is the deliverable: say the row failed, do not hide it
        print(f"khunkhaar git analyze: the artifact was written but the metadata row failed: "
              f"{type(exc).__name__}: {exc}")
    return str(target)
