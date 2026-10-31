"""Read-only git access.

Two properties are enforced here rather than trusted from callers:

1. Only subcommands on the configured read-only allowlist (intersected with a hard floor in
   `core/shared/config.py`) can be executed, and every ref is validated before it reaches argv.
   `--end-of-options` is used wherever git supports it, so a ref named `--help` or `-push` is a
   name, never a flag.
2. The repository's index and working tree must be byte-for-byte as untouched as git reports them.
   `--no-optional-locks` stops git from refreshing the index while reading, and the porcelain
   status plus HEAD are compared before and after every command; a difference aborts the analysis
   instead of continuing with a repository KHUNKHAAR may have changed.

Nothing in this package can write. There is no code path that takes `commit`, `checkout`, `reset`,
`clean`, `stash`, `merge`, `fetch`, `push` or any other mutating subcommand, because they are not
in the floor and cannot be added to it by configuration.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.shared.config import current

# A ref/branch/commit is a name, not an expression. `~` and `^` cover ancestor syntax, `@{...}`
# covers reflogs, and `:` is needed for `<rev>:<path>` blob lookups.
_REF_PATTERN = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@{}/\-^~:]*$")
_MAX_ARG_BYTES = 4096

# git rev-parse echoes arguments it cannot resolve instead of swallowing the separator, so
# `--end-of-options` would corrupt its output. Those calls rely on validation alone (a validated
# rev can never start with '-'), which is why the set is explicit rather than universal.
_NO_END_OF_OPTIONS = frozenset({"rev-parse", "config", "cat-file"})

# Checked in this order: a rebase conflict can leave a CHERRY_PICK_HEAD behind, and calling that
# a cherry-pick would name the wrong operation.
_OPERATION_MARKERS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase or am"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)

_CREDENTIAL_IN_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@]*@")


class GitError(RuntimeError):
    """A git invocation failed or was refused."""


class RepositoryUntrustedError(GitError):
    pass


class StateChangedError(GitError):
    """git reported a different index/HEAD after the command than before it."""


@dataclass
class GitResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def lines(self) -> list[str]:
        return self.stdout.splitlines()

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def records(self, sep: str = "\x1e") -> list[str]:
        """Split a `--format=` stream produced with `%x1e` record terminators.

        git writes a newline after each terminator, so every record but the first would otherwise
        begin with it.
        """
        return [record.strip() for record in self.stdout.split(sep) if record.strip()]

    def fields(self, sep: str = "\x1f") -> list[str]:
        return [part.strip() for part in self.stdout.split(sep)]


def validate_ref(ref: str, *, what: str = "ref", allow_range: bool = False) -> str:
    """Reject anything that is not a plain revspec before it ever reaches argv."""
    text = (ref or "").strip()
    if not text:
        raise GitError(f"empty {what}")
    if len(text.encode()) > _MAX_ARG_BYTES:
        raise GitError(f"{what} is unreasonably long")
    if text.startswith("-"):
        raise GitError(f"{what} may not start with '-': {text!r}")
    if not allow_range and ".." in text:
        raise GitError(f"{what} may not contain '..': {text!r}")
    if not _REF_PATTERN.match(text):
        raise GitError(f"{what} contains unsupported characters: {text!r}")
    if any(part in ("", ".") for part in text.split("/")):
        raise GitError(f"{what} is not a well-formed name: {text!r}")
    _check_blob_path(text)
    return text


def _check_blob_path(text: str) -> None:
    """`<rev>:<path>` is a read of a tracked blob; keep the path inside the repository."""
    _, separator, path = text.partition(":")
    if not separator:
        return
    if text.count(":") != 1 or not path or path.startswith("/"):
        raise GitError(f"blob reference must be <rev>:<relative-path>: {text!r}")
    if ".." in path.split("/"):
        raise GitError(f"blob path may not traverse the repository: {text!r}")


def validate_path(path: str) -> str:
    """A pathspec is repo-relative and cannot escape the repository or look like a flag."""
    text = (path or "").strip()
    if not text:
        raise GitError("empty pathspec")
    if len(text.encode()) > _MAX_ARG_BYTES:
        raise GitError("pathspec is unreasonably long")
    if text.startswith("-") or text.startswith("/") or "\\" in text:
        raise GitError(f"pathspec is not a repository-relative name: {path!r}")
    if any(part == ".." for part in text.split("/")):
        raise GitError(f"pathspec may not traverse the repository: {path!r}")
    return text


def validate_range(spec: str) -> tuple[str, str]:
    """Split `<base>..<head>` (or the 3-dot form) and validate both ends."""
    text = (spec or "").strip()
    separator = "..." if "..." in text else ".."
    if separator not in text:
        raise GitError(f"range must be <base>{separator}<head>, got {spec!r}")
    base, _, head = text.partition(separator)
    return (validate_ref(base.strip(), what="range base"),
            validate_ref(head.strip(), what="range head"))


def redact_url(url: str) -> str:
    """Strip credentials from a remote URL. A URL may carry `user:token@host`; the token never
    leaves the repository, not even into a local report."""
    if not _CREDENTIAL_IN_URL.match(url):
        return url
    scheme, _, rest = url.partition("://")
    _, _, host_and_path = rest.partition("@")
    return f"{scheme}://<redacted>@{host_and_path}"


_OCTAL = re.compile(rb"\\([0-7]{1,3})")
_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
              "\\": "\\", '"': '"'}


def unquote(text: str) -> str:
    """Undo git's C-style quoting (`core.quotePath`) so non-ASCII names read correctly.

    The octal escapes are bytes, not characters: `\\303\\251` is one UTF-8 `e-acute`, so the
    unescaped value is reassembled as bytes and decoded once at the end.
    """
    if not (len(text) >= 2 and text.startswith('"') and text.endswith('"')):
        return text
    raw = text[1:-1].encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] != 0x5C:  # not a backslash
            out.append(raw[i])
            i += 1
            continue
        match = _OCTAL.match(raw, i)
        if match:
            out.append(int(match.group(1), 8))
            i = match.end()
            continue
        nxt = chr(raw[i + 1]) if i + 1 < len(raw) else ""
        escape = _C_ESCAPES.get(nxt, nxt)
        out.extend(escape.encode("utf-8", "surrogateescape"))
        i += 2
    return out.decode("utf-8", "replace")


_TRAILER = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9-]*):[ \t]+(?P<value>\S.*)$")


def parse_trailers(body: str) -> dict[str, list[str]]:
    """Trailers as git defines them: the last paragraph, made only of `Token: value` lines.

    Scanning the whole message would turn prose in a middle paragraph (`Fixes: the crash`) into a
    claim about review metadata, so a block that is not entirely trailers is not a trailer block
    and is reported as none.
    """
    lines = body.rstrip().splitlines()
    block: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            break
        block.append(line)
    trailers: dict[str, list[str]] = {}
    for line in reversed(block):
        if line[:1] in (" ", "\t"):
            continue  # a folded continuation of the trailer above it
        match = _TRAILER.match(line)
        if not match:
            return {}
        trailers.setdefault(match.group("key").lower(), []).append(match.group("value").strip())
    return trailers


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",   # never prompt for credentials: a hung QA run is worse
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
        "HOME": env.get("HOME", "/"),
    })
    # No GIT_* override may smuggle an option - or a credential helper - through the environment.
    for key in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND", "GIT_ASKPASS"):
        env.pop(key, None)
    return env


class GitRepository:
    """One repository, addressed read-only."""

    def __init__(self, root: Path, *, allowlist: frozenset[str] | None = None,
                 verify_untouched: bool | None = None):
        self.root = Path(root).expanduser().resolve()
        settings = current()
        self.allowlist = allowlist if allowlist is not None else settings.git_commands()
        self.verify_untouched = (settings.get("git", "verify_index_untouched", True)
                                 if verify_untouched is None else verify_untouched)
        self.reads = 0

    # ---------------------------------------------------------------- safety
    def snapshot(self) -> tuple[str, str]:
        """(porcelain v2 status, HEAD) - the two things that must not move."""
        status = self._execute("status", ["--porcelain=v2", "--branch"], check=False)
        head = self._execute("rev-parse", [], check=False, revs=["HEAD"])
        return status.stdout, head.stdout.strip()

    def _base_argv(self, subcommand: str, options: list[str], revs: list[str],
                   pathspec: list[str] | None = None) -> list[str]:
        if subcommand not in self.allowlist:
            raise GitError(
                f"git subcommand {subcommand!r} is not on the read-only allowlist "
                f"(configured: {sorted(self.allowlist)})")
        guarded_options: list[str] = []
        for option in options:
            if not option.startswith("-"):
                raise GitError(f"internal option {option!r} must start with '-'")
            if len(option.encode()) > _MAX_ARG_BYTES:
                raise GitError(f"option is unreasonably long: {option[:40]!r}")
            guarded_options.append(option)
        # Revs are the only arguments a caller can influence. They are validated, and
        # `--end-of-options` makes git treat them as names even if they looked like flags.
        guarded_revs = [validate_ref(rev, allow_range=True) for rev in revs]
        guarded_paths = [validate_path(p) for p in (pathspec or [])]
        argv = ["git", "--no-optional-locks", "--no-pager", "-C", str(self.root), subcommand]
        argv += guarded_options
        if guarded_revs and subcommand not in _NO_END_OF_OPTIONS:
            argv += ["--end-of-options", *guarded_revs]
        elif guarded_revs:
            argv += guarded_revs
        if guarded_paths:
            argv += ["--", *guarded_paths]
        return argv

    def run(self, subcommand: str, *, options: list[str] | None = None,
             revs: list[str] | None = None, pathspec: list[str] | None = None,
             check: bool = True, timeout: float = 60.0) -> GitResult:
        argv = self._base_argv(subcommand, list(options or []), list(revs or []),
                               list(pathspec or []))
        before = self.snapshot() if self.verify_untouched else ("", "")
        result = self._spawn(argv, timeout)
        if self.verify_untouched:
            after = self.snapshot()
            if before != after:
                raise StateChangedError(
                    f"repository state changed while reading it (git {subcommand}); aborting "
                    f"before any conclusion is drawn from this analysis")
        if check and not result.ok:
            raise GitError(f"git {subcommand} failed ({result.returncode}): "
                           f"{result.stderr.strip()[:200]}")
        return result

    def _spawn(self, argv: list[str], timeout: float) -> GitResult:
        self.reads += 1
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,  # nosec B603
                                  timeout=timeout, env=_env())
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {subcommand_of(argv)} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GitError(f"git is not usable: {exc}") from exc
        return GitResult(argv=argv, returncode=proc.returncode,
                         stdout=proc.stdout or "", stderr=proc.stderr or "")

    def _execute(self, subcommand: str, options: list[str], *, check: bool,
                 revs: list[str] | None = None) -> GitResult:
        """Internal read that skips the invariance snapshot (it *is* the snapshot)."""
        return self._spawn(self._base_argv(subcommand, options, list(revs or [])), timeout=30.0)

    # ---------------------------------------------------------------- identity
    def is_worktree(self) -> bool:
        try:
            result = self._execute("rev-parse", ["--is-inside-work-tree"], check=False)
        except GitError:
            return False
        return result.ok and result.stdout.strip().splitlines()[:1] == ["true"]

    def require_worktree(self) -> None:
        if not self.root.is_dir():
            raise RepositoryUntrustedError(f"{self.root} is not a directory")
        if not self.is_worktree():
            raise RepositoryUntrustedError(f"{self.root} is not a git worktree")

    def toplevel(self) -> str:
        return self._execute("rev-parse", ["--show-toplevel"], check=True).stdout.strip()

    def head(self) -> str:
        return self._execute("rev-parse", [], check=True, revs=["HEAD"]).stdout.strip()

    def short_head(self) -> str:
        return self.head()[:10]

    def current_branch(self) -> str:
        result = self._execute("rev-parse", ["--abbrev-ref"], check=True, revs=["HEAD"])
        name = result.stdout.strip()
        return "(detached)" if name == "HEAD" else name

    def is_detached(self) -> bool:
        return self.current_branch() == "(detached)"

    def operation_in_progress(self) -> str | None:
        """The multi-step operation git is in the middle of, from its own marker files.

        `rev-parse --git-path` is used instead of assuming `.git/`, because in a linked worktree
        the markers live elsewhere. A report that says only "detached HEAD" about a paused rebase
        is a wrong answer, not an incomplete one.
        """
        for marker, label in _OPERATION_MARKERS:
            try:
                result = self._execute("rev-parse", ["--git-path"], check=False, revs=[marker])
            except GitError:
                continue
            path = result.stdout.strip()
            if result.ok and path and Path(self.root / path).exists():
                return label
        return None

    def remote_url(self, remote: str = "origin") -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", remote):
            raise GitError(f"remote name is not usable: {remote!r}")
        result = self._execute("config", ["--get"], check=False, revs=[f"remote.{remote}.url"])
        if not result.ok:
            return None
        url = result.stdout.strip()
        return redact_url(url) if url else None

    def verify_ref(self, ref: str) -> str | None:
        """Resolve a ref to a commit, or None if it does not exist locally."""
        name = validate_ref(ref)
        result = self._execute("rev-parse", ["--verify", "--quiet"], check=False, revs=[name])
        if not result.ok:
            return None
        resolved = result.stdout.strip()
        # A ref that resolves to several lines (e.g. a tree-ish) is not a commit we can diff.
        if len(resolved.splitlines()) != 1 or not re.fullmatch(r"[0-9a-f]{7,40}", resolved):
            return None
        return resolved

    def rev_parse(self, argument: str) -> str | None:
        return self.verify_ref(argument)

    def resolve_base(self, explicit: str | None = None) -> tuple[str | None, str]:
        """Pick the comparison base: an explicit ref, then the configured candidates.

        Returns (ref, how). `how` is what the report says so a reader knows whether the base was
        chosen by the operator or guessed from a branch name.
        """
        if explicit:
            resolved = self.verify_ref(explicit)
            if resolved is None:
                raise GitError(f"base ref {explicit!r} does not resolve in this repository")
            return explicit, "explicit"
        for candidate in current().get("git", "base_branch_candidates", []) or []:
            name = str(candidate)
            if self.verify_ref(name):
                return name, "candidate"
        return None, "none"

    # ---------------------------------------------------------------- readings
    def branch_names(self) -> list[str]:
        """Local and remote-tracking branch names, without contacting any remote."""
        result = self._execute("for-each-ref", ["--format=%(refname:short)"], check=True,
                               revs=["refs/heads", "refs/remotes"])
        return [line for line in result.lines if line]

    def ahead_behind(self, head: str, base: str) -> tuple[int, int]:
        """`<ahead, behind>` from base to head - a local count of local commits."""
        result = self._execute("rev-list", ["--left-right", "--count"], check=False,
                               revs=[f"{validate_ref(base)}...{validate_ref(head)}"])
        if not result.ok:
            return 0, 0
        parts = result.stdout.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return 0, 0
        behind, ahead = (int(parts[0]), int(parts[1]))
        return ahead, behind

    def merge_base(self, a: str, b: str) -> str | None:
        result = self._execute("merge-base", [], check=False,
                               revs=[validate_ref(a), validate_ref(b)])
        if not result.ok:
            return None
        base = result.stdout.strip()
        return base if re.fullmatch(r"[0-9a-f]{7,40}", base) else None

    def is_ancestor(self, earlier: str, later: str) -> bool:
        result = self._execute("merge-base", ["--is-ancestor"], check=False,
                               revs=[validate_ref(earlier), validate_ref(later)])
        return result.ok

    def commit_subject(self, rev: str) -> str:
        name = validate_ref(rev)
        result = self._execute("log", ["--max-count=1", "--format=%s", "--no-color"],
                               check=False, revs=[name])
        return result.stdout.strip() if result.ok else ""

    def commits(self, head: str = "HEAD", *, base: str | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
        """Commit metadata for a range (or the history behind `head`), newest first.

        `trailers`, `signed_off` and `co_authors` come from the message body, not from `%s`: a
        sign-off is a statement in the commit, and reporting it as present because a subject
        mentioned it would be wrong.
        """
        limit = max(1, min(int(limit), 2000))
        revs = ([f"{validate_ref(base)}..{validate_ref(head)}"] if base
                else [validate_ref(head)])
        fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1f%P%x1e"
        result = self.run("log", options=[f"--max-count={limit}", f"--format={fmt}",
                                          "--no-color", "--date-order"],
                          revs=revs, check=False, timeout=60.0)
        out: list[dict[str, Any]] = []
        for record in result.records():
            parts = record.split("\x1f")
            if len(parts) < 8 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                continue
            parents = parts[7]
            trailers = parse_trailers(parts[6])
            out.append({
                "sha": parts[0], "short": parts[1], "author": parts[2], "email": parts[3],
                "date": parts[4], "subject": parts[5], "parents": parents,
                "is_merge": len(parents.split()) > 1,
                "trailers": trailers,
                "signed_off": bool(trailers.get("signed-off-by")),
                "co_authors": list(trailers.get("co-authored-by", [])),
            })
        return out

    def commit_files(self, head: str = "HEAD", *, base: str | None = None,
                     limit: int = 200) -> dict[str, list[dict[str, str | None]]]:
        """Changed paths per commit, from one `git log --name-status -M` read.

        A merge commit has no entry of its own: git does not diff a merge against a parent unless
        one is chosen, and choosing one here would be a different claim than the one asked for.
        """
        limit = max(1, min(int(limit), 2000))
        revs = ([f"{validate_ref(base)}..{validate_ref(head)}"] if base
                else [validate_ref(head)])
        result = self.run("log", options=[f"--max-count={limit}", "--name-status", "-M",
                                          "--format=%x1e%H", "--no-color", "--date-order"],
                          revs=revs, check=False, timeout=60.0)
        if not result.ok:
            return {}
        out: dict[str, list[dict[str, str | None]]] = {}
        for chunk in result.stdout.split("\x1e"):
            lines = [line for line in chunk.splitlines() if line.strip()]
            if not lines or not re.fullmatch(r"[0-9a-f]{40}", lines[0].strip()):
                continue
            entries: list[dict[str, str | None]] = []
            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                if parts[0][:1] in ("R", "C") and len(parts) >= 3:
                    entries.append({"status": parts[0], "path": unquote(parts[2]),
                                    "old_path": unquote(parts[1])})
                else:
                    entries.append({"status": parts[0], "path": unquote(parts[1]),
                                    "old_path": None})
            out[lines[0].strip()] = entries
        return out


    def status_porcelain(self) -> list[str]:
        return self._execute("status", ["--porcelain=v2", "--branch"], check=True).lines

    def untracked(self) -> list[str]:
        return [line[2:] for line in self.status_porcelain() if line.startswith("? ")]

    def unmerged(self) -> list[str]:
        """Paths with conflict entries in the index - the authoritative conflict signal."""
        found: set[str] = set()
        for line in self._execute("ls-files", ["--unmerged"], check=True).lines:
            head, separator, path = line.partition("\t")  # `<mode> <sha> <stage>\t<file>`
            if separator and head.split()[-1] in ("1", "2", "3"):
                found.add(unquote(path))
        for line in self.status_porcelain():
            if line.startswith("u "):
                # u <XY> <sub> <mode x4> <sha x3> <path>: eleven fields, path last
                parts = line.split(maxsplit=10)
                if len(parts) == 11:
                    found.add(unquote(parts[10]))
        return sorted(found)

    def diff_name_status(self, source: str, target: str | None = None, *,
                         paths: list[str] | None = None) -> list[str]:
        revs = [validate_ref(source)]
        if target:
            revs.append(validate_ref(target))
        return self.run("diff", options=["--name-status", "-M", "--no-color",
                                         "--no-ext-diff", "--no-textconv"],
                        revs=revs, pathspec=paths or None).lines

    def diff_numstat(self, source: str, target: str | None = None, *,
                     paths: list[str] | None = None) -> list[str]:
        revs = [validate_ref(source)]
        if target:
            revs.append(validate_ref(target))
        return self.run("diff", options=["--numstat", "-M", "--no-color", "--no-ext-diff"],
                        revs=revs, pathspec=paths or None).lines

    def diff_patch(self, source: str, target: str | None = None, *, context: int = 3,
                   paths: list[str] | None = None,
                   options: list[str] | None = None) -> str:
        revs = [validate_ref(source)]
        if target:
            revs.append(validate_ref(target))
        opts = ["-M", "--no-color", "--no-ext-diff", "--no-textconv",
                f"--unified={max(0, int(context))}"]
        opts += list(options or [])
        result = self.run("diff", options=opts, revs=revs, pathspec=paths or None,
                          timeout=120.0)
        return result.stdout

    def blob(self, rev_path: str, *, limit: int = 2_000_000) -> str:
        """Contents of a tracked file at a rev. `<rev>:<path>`, validated by validate_ref."""
        name = validate_ref(rev_path)
        result = self.run("show", options=["--no-color"], revs=[name], check=False,
                          timeout=60.0)
        return result.stdout[:limit] if result.ok else ""

    def tracked_files(self, rev: str = "HEAD") -> list[str]:
        result = self.run("ls-tree", options=["-r", "--name-only", "--no-color"],
                          revs=[validate_ref(rev)], check=False)
        return result.lines if result.ok else []

    def count_commits(self, source: str, target: str) -> int:
        result = self.run("rev-list", options=["--count"], check=False,
                          revs=[f"{validate_ref(source)}..{validate_ref(target)}"])
        text = result.stdout.strip()
        return int(text) if result.ok and text.isdigit() else 0

    def total_commits(self) -> int:
        result = self.run("rev-list", options=["--count"], check=False, revs=["HEAD"])
        text = result.stdout.strip()
        return int(text) if result.ok and text.isdigit() else 0

    def parents(self, rev: str) -> list[str]:
        """Parent commit SHAs. Empty for a root commit, two or more for a merge."""
        name = validate_ref(rev)
        result = self.run("log", options=["--max-count=1", "--format=%P", "--no-color"],
                          revs=[name], check=False)
        return result.stdout.split() if result.ok else []

    # `git show` resolves a root commit against the empty tree itself, so commit-level analysis
    # uses these instead of `diff <parent> <commit>` - which would need a magic SHA.
    def show_name_status(self, rev: str) -> list[str]:
        return self._show(["--name-status", "-M"], rev).lines

    def show_numstat(self, rev: str) -> list[str]:
        return self._show(["--numstat", "-M"], rev).lines

    def show_patch(self, rev: str, *, context: int = 3) -> str:
        return self._show([f"--unified={max(0, int(context))}", "-M"], rev).stdout

    def _show(self, options: list[str], rev: str) -> GitResult:
        opts = ["--no-color", "--no-ext-diff", "--no-textconv", "--format=%H",
                *options]
        return self.run("show", options=opts, revs=[validate_ref(rev)], timeout=120.0)


def subcommand_of(argv: list[str]) -> str:
    """The git subcommand inside a built argv (position 5, after `-C <root>`), for messages."""
    return argv[5] if len(argv) > 5 else "git"


def open_repository(root: Path | str) -> GitRepository:
    """A repository that is on disk and is a git worktree, or a clear refusal."""
    repo = GitRepository(Path(str(root)))
    repo.require_worktree()
    return repo
