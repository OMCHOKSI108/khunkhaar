"""Fixtures for the Phase 3 suite.

`kh_root` isolates storage: `paths.kh_root()` re-reads $KHUNKHAAR_ROOT on every call, so pointing
it at a tmp directory keeps these tests off the real platform database - but the config cache is
process-wide, so it is reset here and restored afterwards.

`git_repo` builds throwaway repositories under tmp_path. Every repository used by the git tests is
created here, never discovered, so a test can assert an exact file, rename, delete, merge or
conflict state instead of hoping a real checkout still looks the way it did.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404
from pathlib import Path

import pytest

from core.shared import config as config_module

GIT_APP = "def keep():\n    pass\n\n\ndef target(a):\n    return a\n"
GIT_APP_CHANGED = ("def keep():\n    pass\n\n\ndef target(a, b=2):\n    if b:\n"
                   "        return a + b\n    return a\n")
GIT_APP_STAGED = ("def keep():\n    pass\n\n\ndef target(a, b=2):\n    # staged edit\n"
                  "    return a + b\n")
# A .py file that is not valid Python 3: attribution must report 'unknown', never 'no symbols'.
BROKEN_PY = "def broken(:\n    return 1\n"

# A module whose definitions nest, decorate and rename, so symbol attribution has something real
# to be wrong about: `strip_chunk` sits inside `Store.load`, and `@staticmethod` belongs to it.
PY_STORE = '''"""Store."""


class Store:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def load(text):
        def strip_chunk(chunk):
            return chunk.strip()
        return strip_chunk(text)

    def save(self, value):
        return str(value)


def unused():
    return None
'''

# One edit inside each of two definitions, plus `unused` renamed away and `flush` renamed in.
PY_STORE_EDITED = '''"""Store."""


class Store:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def load(text):
        def strip_chunk(chunk):
            return chunk.strip().lower()
        return strip_chunk(text)

    def save(self, value):
        return str(value).strip()


def flush():
    return 0
'''

# Identical to PY_STORE_EDITED apart from the decorator on line 8.
PY_STORE_DECORATED = PY_STORE_EDITED.replace("@staticmethod", "@classmethod")


class GitFixture:
    """One synthetic repository plus the git-side facts the tests assert on."""

    def __init__(self, root: Path):
        self.root = root
        self.branches: dict[str, str] = {}
        self.base_sha = ""
        self.merge_sha = ""
        self.shas: dict[str, str] = {}

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(self.root), check=check,
                              capture_output=True, text=True)

    def sha(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def commit(self, message: str, *args: str) -> str:
        """Commit and remember the SHA by message, so a test can name the change it expects."""
        self.git("commit", *args, "-m", message)
        sha = self.sha()
        self.shas[message] = sha
        return sha

    def write(self, name: str, text: str) -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def state(self) -> str:
        """A digest of the index and of every tracked file's mtime and content.

        Two snapshots being equal is the operational definition of "KHUNKHAAR did not touch this
        repository" - it catches an index refresh, a checkout, a stash or a rewrite, none of which
        a stdout comparison would notice.
        """
        digest = hashlib.sha256()
        index = self.root / ".git" / "index"
        if index.is_file():
            digest.update(b"index\x00")
            digest.update(index.read_bytes())
        for path in sorted(p for p in self.root.rglob("*")
                           if p.is_file() and ".git" not in p.parts):
            rel = path.relative_to(self.root).as_posix().encode()
            stat = path.stat()
            digest.update(rel + b"\x00" + str(stat.st_mtime_ns).encode() + b"\x00"
                          + path.read_bytes() + b"\x00")
        return digest.hexdigest()


def _require_git() -> None:
    """These tests verify git behaviour, so without git there is nothing to conclude."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed in this environment")


def _init(name: str, root: Path) -> GitFixture:
    _require_git()
    repo = GitFixture(root / name)
    repo.root.mkdir(parents=True)
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.email", "qa@example.invalid")
    repo.git("config", "user.name", "QA Fixture")
    repo.git("config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def git_repo(tmp_path) -> GitFixture:
    """main with one clean feature branch: modify, rename, delete, add, binary, plus a merge."""
    repo = _init("app", tmp_path)
    repo.write("src/app.py", GIT_APP)
    repo.write("cfg.yaml", "a: 1\n")
    repo.write("legacy.txt", "old name\n")
    repo.write("README.md", "# fixture\n\nfirst paragraph\n\nlast paragraph\n")
    repo.git("add", "-A")
    base = repo.commit("base commit", "-q")

    repo.git("checkout", "-q", "-b", "feature/payments")
    repo.write("src/app.py", GIT_APP_CHANGED)
    repo.git("mv", "legacy.txt", "current.txt")
    repo.git("add", "-A")
    repo.commit("change target signature and rename legacy", "-q")
    repo.write("gone.txt", "temporary\n")
    repo.git("add", "gone.txt")
    repo.commit("add the scratch file", "-q")
    repo.git("rm", "-q", "gone.txt")
    repo.commit("delete the scratch file", "-q")
    (repo.root / "bin.dat").write_bytes(b"\x00\x01\x02\x03binary\n")
    repo.git("add", "bin.dat")
    repo.commit("add a binary asset", "-q")
    repo.write("README.md", "# fixture\n\nfirst paragraph\n\nmiddle paragraph\n\nlast paragraph\n")
    repo.commit("reflow the readme", "-qa")
    feature_tip = repo.sha()

    repo.git("checkout", "-q", "main")
    repo.write("cfg.yaml", "a: 2\n")
    main_tip = repo.commit("main moves on", "-qa")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature/payments into main",
             "feature/payments")
    merge = repo.sha()
    repo.shas["merge feature into main"] = merge

    repo.branches = {"main": main_tip, "feature/payments": feature_tip}
    repo.base_sha = base
    repo.merge_sha = merge
    return repo


@pytest.fixture
def dirty_repo(git_repo: GitFixture) -> GitFixture:
    """The same repository with a staged edit, an unstaged edit and an untracked file."""
    git_repo.write("src/app.py", GIT_APP_STAGED)
    git_repo.git("add", "src/app.py")
    git_repo.write("cfg.yaml", "a: 3\n")
    git_repo.write("scratch.txt", "untracked\n")
    return git_repo


@pytest.fixture
def py_repo(tmp_path) -> GitFixture:
    """Four Python-shaped commits: a root add, an edit, a decorator-only edit, a broken file.

    Line numbers are part of the fixture, so an attribution test can assert that a change is
    credited to definition X at line N rather than to whatever git printed above the hunk.
    """
    repo = _init("symbols", tmp_path)
    repo.write("src/store.py", PY_STORE)
    repo.git("add", "-A")
    repo.base_sha = repo.commit("add store", "-q")
    repo.write("src/store.py", PY_STORE_EDITED)
    repo.commit("edit definitions and rename one away", "-qa")
    repo.write("src/store.py", PY_STORE_DECORATED)
    repo.commit("change only a decorator", "-qa")
    repo.write("templates/legacy.py", BROKEN_PY)
    repo.git("add", "-A")
    repo.commit("add a file that is not valid python 3", "-q")
    return repo


@pytest.fixture
def impact_repo(tmp_path) -> GitFixture:
    """A layered application plus one commit that changes only the service class."""
    repo = _init("impact-app", tmp_path)
    repo.write("app/__init__.py", "")
    repo.write("app/models.py", IMPACT_MODELS)
    repo.write("app/service.py", IMPACT_SERVICE)
    repo.write("app/main.py", IMPACT_MAIN)
    repo.write("tests/test_service.py", IMPACT_TEST)
    repo.write("web/helper.ts", IMPACT_TS_HELPER)
    repo.write("web/client.ts", IMPACT_TS_CLIENT)
    repo.write("web/users.controller.ts", IMPACT_NEST)
    repo.write("legacy/tool.rb", IMPACT_RUBY)
    repo.write("docs/requirements.md", IMPACT_RULES)
    repo.write("README.md", "# impact fixture\n")
    repo.write(".env", "PORT=8080\nSECRET_KEY=smoke-value-not-a-real-secret\n")
    repo.git("add", "-A")
    repo.base_sha = repo.commit("initial application", "-q")
    repo.write("app/service.py", IMPACT_SERVICE_CHANGED)
    repo.git("add", "-A")
    repo.shas["add a rename method"] = repo.commit("add a rename method", "-q")
    return repo


@pytest.fixture
def mr_repo(tmp_path) -> GitFixture:
    """main and a feature branch that diverged, with main moving afterwards.

    The feature tip is checked out, so the dependency graph matches the merge-request source
    revision. main's README commit is a trap for two-dot comparisons: a merge-base review must not
    list it, because it is not part of what the feature branch adds.
    """
    repo = _init("mr-app", tmp_path)
    repo.write("app/__init__.py", "")
    repo.write("app/models.py", IMPACT_MODELS)
    repo.write("app/service.py", IMPACT_SERVICE)
    repo.write("app/main.py", IMPACT_MAIN)
    repo.write("tests/test_service.py", IMPACT_TEST)
    repo.write("README.md", "# mr fixture\n")
    repo.write(".env", "PORT=8080\nSECRET_KEY=smoke-value-not-a-real-secret\n")
    repo.git("add", "-A")
    repo.base_sha = repo.commit("base application", "-q")

    repo.git("checkout", "-q", "-b", "feature/items")
    repo.write("app/service.py", IMPACT_SERVICE_CHANGED)
    repo.git("add", "-A")
    repo.shas["rename service method"] = repo.commit("rename service method", "-q")
    feature_tip = repo.sha()

    repo.git("checkout", "-q", "main")
    repo.write("README.md", "# mr fixture\n\nmain moved after the branch point\n")
    repo.git("add", "-A")
    repo.shas["main docs move"] = repo.commit("main docs move", "-qa")
    main_tip = repo.sha()

    repo.git("checkout", "-q", "feature/items")
    repo.branches = {"main": main_tip, "feature/items": feature_tip}
    return repo


# A FastAPI-shaped application whose layers call each other, so the dependency graph has a real
# chain to walk: route -> handler -> service class -> ORM entity, plus a test that imports the
# service, a TypeScript pair, a NestJS controller, an unparsed Ruby file and a documented rule.
# `.env` holds a fake secret and exists only to prove that its values are never read.
IMPACT_MODELS = '''from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String

Base = declarative_base()


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    name = Column(String)
'''

IMPACT_SERVICE = '''from app.models import Item


class ItemService:
    def create(self, name: str) -> Item:
        item = Item()
        item.name = name
        return item

    def delete(self, item: Item) -> None:
        item.delete()
'''

IMPACT_SERVICE_CHANGED = IMPACT_SERVICE + '''
    def rename(self, item: Item, name: str) -> Item:
        item.name = name
        return item
'''

IMPACT_MAIN = '''from fastapi import Depends, FastAPI

from app.service import ItemService

app = FastAPI()


def get_current_user():
    return {"id": 1}


@app.get("/items")
def list_items(user=Depends(get_current_user)):
    return ItemService().create("x")


@app.post("/items")
def create_item(name: str):
    return ItemService().create(name)
'''

IMPACT_TEST = '''from app.service import ItemService


def test_create():
    assert ItemService() is not None
'''

IMPACT_TS_HELPER = '''export function helper(): string {
  return "ok";
}
'''

IMPACT_TS_CLIENT = '''import { helper } from "./helper";

export function run(): string {
  return helper();
}
'''

IMPACT_NEST = '''import { Controller, Get, Param } from "@nestjs/common";

@Controller("users")
export class UsersController {
  @Get(":id")
  findOne(@Param("id") id: string) {
    return { id };
  }
}
'''

IMPACT_RUBY = '''def run
  puts "no parser for ruby is bundled"
end
'''

IMPACT_RULES = """# Business rules

- RULE R1: A user must never read another user's items.
"""


def _conflict(root: Path, filename: str = "cfg.yaml") -> GitFixture:
    """A real mid-merge conflict: the index carries unmerged entries, not just marker text.

    `filename` is parametrised because git quotes a non-ASCII path in every machine-readable
    listing, and that quoting has to be undone before a path can be compared or reported.
    """
    safe = "".join(char if char.isalnum() else "-" for char in filename)
    repo = _init(f"conflict-{safe}", root)
    repo.write(filename, "a: 1\n")
    repo.git("add", "-A")
    repo.commit("base", "-q")
    repo.git("checkout", "-q", "-b", "side")
    repo.write(filename, "a: side\n")
    repo.commit("side changes a", "-qa")
    repo.git("checkout", "-q", "main")
    repo.write(filename, "a: main\n")
    repo.commit("main changes a", "-qa")
    repo.git("merge", "side", check=False)  # expected to fail: that is the fixture
    return repo


@pytest.fixture
def conflicted_repo(tmp_path) -> GitFixture:
    return _conflict(tmp_path)


@pytest.fixture
def conflicted_repo_utf8(tmp_path) -> GitFixture:
    return _conflict(tmp_path, "café.yaml")


@pytest.fixture
def rebasing_repo(tmp_path) -> GitFixture:
    """A real rebase stopped on a conflict: a rebase-merge marker and unmerged index entries."""
    repo = _init("rebasing", tmp_path)
    repo.write("cfg.yaml", "a: 1\n")
    repo.git("add", "-A")
    repo.commit("base", "-q")
    repo.git("checkout", "-q", "-b", "topic")
    repo.write("cfg.yaml", "a: topic\n")
    repo.commit("topic changes a", "-qa")
    repo.git("checkout", "-q", "main")
    repo.write("cfg.yaml", "a: main\n")
    repo.commit("main changes a", "-qa")
    repo.git("checkout", "-q", "topic")
    repo.git("rebase", "main", check=False)  # expected to stop on the conflict
    return repo


@pytest.fixture
def non_repo(tmp_path) -> Path:
    path = tmp_path / "not-a-repository"
    path.mkdir()
    (path / "README.md").write_text("nothing here\n", encoding="utf-8")
    return path


@pytest.fixture
def kh_root(tmp_path, monkeypatch):
    root = tmp_path / "KHUNKHAAR-TEST"
    root.mkdir()
    for name in ("config", "data/metadata", "evidence", "reports", "logs", "runtime/tmp",
                 "projects/registry", "intelligence"):
        (root / name).mkdir(parents=True, exist_ok=True)
    real_config = config_module.paths.kh_root() / "config" / "khunkhaar.yaml"
    if real_config.is_file():
        (root / "config" / "khunkhaar.yaml").write_text(real_config.read_text(encoding="utf-8"),
                                                        encoding="utf-8")
    monkeypatch.setenv("KHUNKHAAR_ROOT", str(root))
    config_module._CURRENT = None
    yield root
    config_module._CURRENT = None


@pytest.fixture
def patch_config(kh_root):
    """Patch one config section inside an isolated root, so a gate can be tested without editing
    the shipped YAML."""
    import yaml

    def apply(section: str, values: dict):
        path = kh_root / "config" / "khunkhaar.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data.setdefault(section, {}).update(values)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        config_module._CURRENT = None
        return data

    yield apply
    config_module._CURRENT = None
