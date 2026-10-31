"""Synthetic repositories for the Phase 3 acceptance chain.

Both repositories are created by fixture code under a temporary directory with a real `git init`.
No registered project and no real checkout is touched.

`build_py_project` copies the Phase 2 `vulnapp` application into the repository so the same probes
apply unchanged. Its base commit *guards* three handlers (request validation, owner-only read,
submitted-only approval); the feature commit removes those guards, changes the request/owner
relationship in `schema.sql`, touches the workflow document and keeps an existing test scenario.
The story is one merge request that silently re-opens three previously fixed defects.

`build_ts_project` carries the same six conditions in a NestJS/Prisma shape: a validation rule and
a permission check change in the controller, a Prisma relation change, an existing e2e scenario,
and a modified workflow document. It is analysed through git/impact only; no TypeScript runtime is
bundled, so the acceptance suite never claims to execute it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404 - fixture setup with fixed argv
from dataclasses import dataclass, field
from pathlib import Path

VULNAP_SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "vulnapp"

_CREATE_GUARD = '''    if not body.title.strip() or body.amount <= 0:
        raise HTTPException(422, "title must be non-empty and amount must be positive")
'''
_OWNER_GUARD = '''    if row["owner_id"] != user["id"] and user["role"] != "manager":
        raise HTTPException(403, "only the owner may read this request")
'''
_STATE_GUARD = '''    if row["status"] != "submitted":
        conn.commit()
        conn.close()
        raise HTTPException(409, "only a submitted request can be approved")
'''


@dataclass
class AcceptanceRepo:
    root: Path
    base_sha: str = ""
    head_sha: str = ""
    branch: str = "main"
    shas: dict[str, str] = field(default_factory=dict)

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(self.root), check=check,
                              capture_output=True, text=True)

    def write(self, name: str, text: str) -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def commit(self, message: str, *args: str) -> str:
        self.git("commit", *args, "-m", message)
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.shas[message] = sha
        return sha

    def state(self) -> str:
        """Index + every tracked file's mtime and content; equality means untouched."""
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


def _init(name: str, parent: Path) -> AcceptanceRepo:
    repo = AcceptanceRepo(parent / name)
    repo.root.mkdir(parents=True)
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.email", "qa@example.invalid")
    repo.git("config", "user.name", "QA Fixture")
    repo.git("config", "commit.gpgsign", "false")
    return repo


def guarded_app_source() -> str:
    """The Phase 2 app with the three guards restored, as the base commit had it.

    Each replacement asserts that it matched, so an edit to the fixture app fails loudly here
    instead of silently producing a repository that no longer tells the intended story.
    """
    source = (VULNAP_SOURCE / "app.py").read_text(encoding="utf-8")
    create_old = ("def create_request(body: RequestIn, user: dict = Depends(current_user)):\n"
                  "    # R4: no validation happens here.\n")
    assert create_old in source
    source = source.replace(
        create_old,
        "def create_request(body: RequestIn, user: dict = Depends(current_user)):\n"
        + _CREATE_GUARD + "    # R4 guard present in this revision.\n", 1)

    read_old = '''def get_request(request_id: int, user: dict = Depends(current_user)):
    # R1: any authenticated user may read any request.
    conn = _conn()
    row = _request_row(conn, request_id)
    conn.close()
    if row is None:
        raise HTTPException(404, "request not found")
    return row
'''
    assert read_old in source
    source = source.replace(
        read_old,
        '''def get_request(request_id: int, user: dict = Depends(current_user)):
    conn = _conn()
    row = _request_row(conn, request_id)
    conn.close()
    if row is None:
        raise HTTPException(404, "request not found")
''' + _OWNER_GUARD + "    return row\n", 1)

    approve_old = '''        raise HTTPException(403, "only the owner or a manager may approve")
    # R3: the state machine is not checked; a draft can be approved directly.
'''
    assert approve_old in source
    source = source.replace(
        approve_old,
        "        raise HTTPException(403, \"only the owner or a manager may approve\")\n"
        + _STATE_GUARD + "    # R3 guard present in this revision.\n", 1)
    return source


def build_py_project(tmp_path: Path) -> AcceptanceRepo:
    repo = _init("py-project", tmp_path)
    for name in ("app.py", "schema.sql", "pyproject.toml", "qa-users.json"):
        shutil.copy2(VULNAP_SOURCE / name, repo.root / name)
    (repo.root / "docs").mkdir()
    shutil.copy2(VULNAP_SOURCE / "docs" / "BUSINESS_RULES.md",
                 repo.root / "docs" / "BUSINESS_RULES.md")
    repo.write("docs/WORKFLOW.md",
               "# Request workflow\n\n1. An employee creates a draft request.\n"
               "2. The employee submits it.\n3. A manager approves a submitted request.\n")
    repo.write("tests/test_app.py",
               '"""Existing regression scenario from the previous release."""\n\n'
               "from app import create_request  # the handler this scenario exercises\n\n\n"
               "def test_negative_amount_is_rejected():\n"
               "    assert callable(create_request)\n")
    repo.write("app.py", guarded_app_source())
    repo.git("add", "-A")
    repo.base_sha = repo.commit("initial application with request guards", "-q")

    repo.git("checkout", "-q", "-b", "feature/relax-request-guards")
    # The merge request: guards removed, relationship changed, validation rule restated.
    shutil.copy2(VULNAP_SOURCE / "app.py", repo.root / "app.py")
    schema = (VULNAP_SOURCE / "schema.sql").read_text(encoding="utf-8")
    schema = schema.replace(
        "owner_id INTEGER NOT NULL REFERENCES users(id),",
        "owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,", 1)
    repo.write("schema.sql", schema)
    repo.write("docs/WORKFLOW.md",
               "# Request workflow\n\n1. An employee creates a draft request.\n"
               "2. A manager may approve any request without an explicit submission step.\n")
    repo.git("add", "-A")
    repo.commit("relax request guards and document the new workflow", "-q")
    repo.head_sha = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.branch = "feature/relax-request-guards"
    return repo


def build_ts_project(tmp_path: Path) -> AcceptanceRepo:
    repo = _init("ts-project", tmp_path)
    repo.write("package.json",
               '{\n  "name": "acceptance-ts-project",\n  "version": "1.0.0",\n'
               '  "private": true,\n  "dependencies": {\n'
               '    "@nestjs/common": "^10.0.0",\n    "@nestjs/core": "^10.0.0",\n'
               '    "@prisma/client": "^5.0.0"\n  }\n}\n')
    repo.write("src/main.ts",
               'import { NestFactory } from "@nestjs/core";\n'
               'import { AppModule } from "./app.module";\n\n'
               "async function bootstrap() {\n"
               "  const app = await NestFactory.create(AppModule);\n"
               '  await app.listen(3000);\n}\n\nbootstrap();\n')
    repo.write("src/app.module.ts",
               'import { Module } from "@nestjs/common";\n'
               'import { UsersController } from "./users/users.controller";\n'
               'import { UsersService } from "./users/users.service";\n\n'
               "@Module({ controllers: [UsersController], providers: [UsersService] })\n"
               "export class AppModule {}\n")
    base_controller = '''import { Body, Controller, Get, Param, Post } from "@nestjs/common";
import { UsersService } from "./users.service";

@Controller("users")
export class UsersController {
  constructor(private readonly users: UsersService) {}

  @Get(":id")
  findOne(@Param("id") id: string) {
    return this.users.findOneForCaller(Number(id));
  }

  @Post()
  create(@Body() body: { name: string; email: string }) {
    if (!body.email.includes("@")) {
      throw new Error("email must be valid");
    }
    return this.users.create(body.name, body.email);
  }
}
'''
    repo.write("src/users/users.controller.ts", base_controller)
    repo.write("src/users/users.service.ts", '''export class UsersService {
  findOneForCaller(id: number) {
    return { id, name: "sample" };
  }

  create(name: string, email: string) {
    return { name, email };
  }
}
''')
    repo.write("prisma/schema.prisma", '''datasource db {
  provider = "sqlite"
  url      = "file:dev.db"
}

model User {
  id    Int    @id @default(autoincrement())
  name  String
  email String @unique
  team  Team?
}

model Team {
  id    Int    @id @default(autoincrement())
  name  String
  users User[]
}
''')
    repo.write("test/users.controller.spec.ts", '''import { UsersController } from "../src/users/users.controller";

describe("users", () => {
  it("rejects an invalid email", () => {
    expect(new UsersController(null as never)).toBeDefined();
  });
});
''')
    repo.write("docs/WORKFLOW.md",
               "# Team management workflow\n\n1. An administrator creates a user in a team.\n"
               "2. A user may read only their own profile.\n")
    repo.git("add", "-A")
    repo.base_sha = repo.commit("initial users module", "-q")

    repo.git("checkout", "-q", "-b", "feature/user-team")
    feature_controller = '''import { Body, Controller, Get, Param, Post } from "@nestjs/common";
import { UsersService } from "./users.service";

@Controller("users")
export class UsersController {
  constructor(private readonly users: UsersService) {}

  @Get(":id")
  findOne(@Param("id") id: string) {
    return this.users.findOneUnscoped(Number(id));
  }

  @Post()
  create(@Body() body: { name: string; email: string }) {
    return this.users.create(body.name, body.email);
  }
}
'''
    repo.write("src/users/users.controller.ts", feature_controller)
    repo.write("src/users/users.service.ts", '''export class UsersService {
  findOneForCaller(id: number) {
    return { id, name: "sample" };
  }

  findOneUnscoped(id: number) {
    return { id, name: "sample", email: "sample@example.invalid" };
  }

  create(name: string, email: string) {
    return { name, email };
  }
}
''')
    repo.write("prisma/schema.prisma", '''datasource db {
  provider = "sqlite"
  url      = "file:dev.db"
}

model User {
  id     Int    @id @default(autoincrement())
  name   String
  email  String @unique
  teamId Int
  team   Team   @relation(fields: [teamId], references: [id])

  @@index([teamId])
}

model Team {
  id    Int    @id @default(autoincrement())
  name  String
  users User[]
}
''')
    repo.write("docs/WORKFLOW.md",
               "# Team management workflow\n\n1. An administrator creates a user in a team.\n"
               "2. Any authenticated caller may read any profile by id.\n")
    repo.git("add", "-A")
    repo.commit("relax profile access and attach users to teams", "-q")
    repo.head_sha = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.branch = "feature/user-team"
    return repo
