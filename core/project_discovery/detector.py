"""Non-destructive project discovery.

Reads a project directory the way a reviewer would - file names, manifests, compose files - and
writes a profile into KHUNKHAAR's own registry. It never executes project scripts, never copies
source code into the QA tree, and never records a secret value: only the *names* of environment
variables and whether a value is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
# Only fixed argv lists, never a shell.
import subprocess  # nosec B404
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.shared import paths

ROOT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "mix.exs",
    "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml",
)

LANGUAGE_BY_EXT = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".py": "Python", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++ header",
    ".hpp": "C++", ".swift": "Swift", ".m": "Objective-C", ".scala": "Scala",
    ".vue": "Vue", ".svelte": "Svelte", ".ex": "Elixir", ".exs": "Elixir",
}

TEST_FILE_PATTERNS = (
    re.compile(r"(^|/)test_[^/]+\.py$"), re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)tests?/.*\.py$"), re.compile(r"(^|/)[^/]+\.(test|spec)\.(js|jsx|ts|tsx)$"),
    re.compile(r"(^|/)__tests__/"), re.compile(r"(^|/)e2e/.*\.(ts|js)$"),
)

DEFAULT_SKIP = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".nuxt",
    "target", "vendor", ".mypy_cache", ".pytest_cache", "coverage", ".idea", ".gradle", "out",
    ".turbo", ".cache", "site-packages",
}


def find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
        if candidate == Path.home():
            return None
    return None


def project_id_for(root: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def _skip_dirs() -> set[str]:
    from core.shared.config import current

    configured = current().get("project_discovery", "skip_dirs", []) or []
    return set(DEFAULT_SKIP) | {str(d) for d in configured}


def _walk(root: Path, max_files: int) -> tuple[Counter, list[Path], int, bool]:
    skip = _skip_dirs()
    languages: Counter = Counter()
    files: list[Path] = []
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if scanned >= max_files:
                truncated = True
                break
            scanned += 1
            path = Path(dirpath) / name
            files.append(path)
            lang = LANGUAGE_BY_EXT.get(path.suffix.lower())
            if lang:
                languages[lang] += 1
        if truncated:
            break
    return languages, files, scanned, truncated


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_NODE_FRAMEWORKS = {
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "@angular/core": "Angular",
    "express": "Express", "@nestjs/core": "NestJS", "svelte": "Svelte", "fastify": "Fastify",
    "jest": "Jest", "vitest": "Vitest", "@playwright/test": "Playwright", "cypress": "Cypress",
    "typescript": "TypeScript",
}

_PY_FRAMEWORKS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "pytest": "Pytest",
    "sqlalchemy": "SQLAlchemy", "celery": "Celery", "uvicorn": "Uvicorn", "tornado": "Tornado",
}


def _frameworks(root: Path) -> list[str]:
    found: set[str] = set()

    package = _read_json(root / "package.json")
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    for name, label in _NODE_FRAMEWORKS.items():
        if name in deps:
            found.add(label)

    for candidate in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py"):
        path = root / candidate
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for name, label in _PY_FRAMEWORKS.items():
            if re.search(rf"\b{re.escape(name)}\b", text):
                found.add(label)

    for candidate, needles in (
        ("pom.xml", ("spring-boot",)),
        ("build.gradle", ("spring-boot",)), ("build.gradle.kts", ("spring-boot",)),
        ("go.mod", ("gin-gonic/gin", "labstack/echo", "gofiber/fiber")),
        ("Gemfile", ("rails",)), ("composer.json", ("laravel/framework",)),
    ):
        path = root / candidate
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if any(n in text for n in needles):
                found.add("Spring Boot" if "spring-boot" in needles else
                          "Gin" if "gin-gonic" in needles else
                          "Echo" if "labstack" in needles else
                          "Fiber" if "gofiber" in needles else
                          "Rails" if "rails" in needles else "Laravel")
    return sorted(found)


def _infrastructure(root: Path) -> dict[str, Any]:
    compose_files = [root / n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
                     if (root / n).is_file()]
    services: list[dict[str, Any]] = []
    databases: set[str] = set()
    for compose in compose_files:
        try:
            doc = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for name, spec in (doc.get("services") or {}).items():
            if not isinstance(spec, dict):
                continue
            image = str(spec.get("image", ""))
            services.append({"name": name, "image": image or None})
            low = image.lower()
            for key, label in (("postgres", "PostgreSQL"), ("mysql", "MySQL"), ("mariadb", "MariaDB"),
                               ("mongo", "MongoDB"), ("redis", "Redis"), ("rabbitmq", "RabbitMQ"),
                               ("kafka", "Kafka"), ("elasticsearch", "Elasticsearch")):
                if key in low:
                    databases.add(label)

    env_names, env_with_values = _env_names(root)
    text_hint = " ".join(sorted(env_names)).lower()
    for key, label in (("postgres", "PostgreSQL"), ("database_url", "PostgreSQL"), ("mysql", "MySQL"),
                       ("mongo", "MongoDB"), ("redis", "Redis")):
        if key in text_hint:
            databases.add(label)

    return {
        "docker": (root / "Dockerfile").is_file() or bool(compose_files),
        "dockerfiles": sorted(p.name for p in root.glob("Dockerfile*") if p.is_file()),
        "compose_files": [p.name for p in compose_files],
        "compose_services": services,
        "databases": sorted(databases),
        "env_variable_names": sorted(env_names),
        "env_values_present": sorted(env_with_values),
    }


def _env_names(root: Path) -> tuple[set[str], set[str]]:
    """Variable names only. Values are reduced to a boolean 'is set' and then discarded."""
    names: set[str] = set()
    with_values: set[str] = set()
    for candidate in (".env", ".env.local", ".env.development", ".env.example", ".env.sample",
                      ".env.test", ".env.production"):
        path = root / candidate
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            names.add(key)
            if value.strip():
                with_values.add(key)
    return names, with_values


def _api_definitions(root: Path, files: list[Path]) -> dict[str, Any]:
    openapi = []
    graphql = []
    postman = []
    for path in files:
        name = path.name.lower()
        if re.match(r"(openapi|swagger)[^/]*\.(json|ya?ml)$", name):
            openapi.append(str(path.relative_to(root)))
        elif path.suffix.lower() in (".graphql", ".gql") or name in ("schema.gql", "schema.graphql"):
            graphql.append(str(path.relative_to(root)))
        elif name.endswith(".postman_collection.json"):
            postman.append(str(path.relative_to(root)))
    bruno = [str(p.relative_to(root)) for p in root.rglob("bruno.json")][:5]
    bruno_collections = [str(p.relative_to(root)) for p in list(root.rglob("*.bru"))[:20]]
    return {
        "openapi": openapi[:10],
        "graphql_schemas": graphql[:10],
        "postman_collections": postman[:10],
        "bruno_collections": bruno + bruno_collections[:5],
    }


def _existing_tests(root: Path, files: list[Path]) -> dict[str, Any]:
    frameworks: set[str] = set()
    if any((root / n).exists() for n in ("pytest.ini", "conftest.py", "tox.ini")):
        frameworks.add("Pytest")
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "pytest" in pyproject.read_text(encoding="utf-8", errors="replace"):
        frameworks.add("Pytest")
    if any((root / n).exists() for n in ("jest.config.js", "jest.config.ts", "jest.config.cjs")):
        frameworks.add("Jest")
    if any((root / n).exists() for n in ("vitest.config.ts", "vitest.config.js", "vitest.config.mts")):
        frameworks.add("Vitest")
    if any((root / n).exists() for n in ("playwright.config.ts", "playwright.config.js")):
        frameworks.add("Playwright")
    if any((root / n).exists() for n in ("cypress.config.ts", "cypress.config.js", "cypress.json")):
        frameworks.add("Cypress")
    test_files = [str(p.relative_to(root)) for p in files
                  if any(pat.search(str(p.relative_to(root))) for pat in TEST_FILE_PATTERNS)]
    return {"frameworks": sorted(frameworks), "test_file_count": len(test_files),
            "sample_test_files": sorted(test_files)[:10]}


def _git(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"is_repo": False}
    info: dict[str, Any] = {"is_repo": True}
    for key, args in (("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
                      ("commit", ["rev-parse", "--short", "HEAD"]),
                      ("remote_count", ["remote"])):
        try:
            # Fixed argv, no shell; `git` comes from PATH by design.
            out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,  # nosec B603 B607
                                 timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0:
            value = out.stdout.strip()
            info[key] = (len([line for line in value.splitlines() if line.strip()])
                         if key == "remote_count" else value)
    return info


def _project_type(languages: Counter, frameworks: list[str], infra: dict, api: dict) -> str:
    if api["openapi"] or api["graphql_schemas"]:
        return "api"
    if {"Next.js", "React", "Vue", "Angular", "Svelte", "Nuxt", "Laravel", "Rails"} & set(frameworks):
        return "web-app"
    if {"FastAPI", "Flask", "Django", "Express", "NestJS", "Spring Boot"} & set(frameworks):
        return "service"
    if infra["compose_files"] and not languages:
        return "infrastructure"
    if languages and not infra["compose_files"]:
        return "library-or-tool"
    return "unknown"


def detect(start: Path | None = None) -> dict[str, Any]:
    from core.shared.config import current

    cwd = (start or Path.cwd()).resolve()
    root = find_project_root(cwd)
    max_files = int(current().get("project_discovery", "max_files_scanned", 40_000))
    if root is None:
        return {"valid_project": False, "cwd": str(cwd),
                "reason": "no project marker found (.git, package.json, pyproject.toml, go.mod, pom.xml, ...)"}

    languages, files, scanned, truncated = _walk(root, max_files)
    infra = _infrastructure(root)
    api = _api_definitions(root, files)
    tests = _existing_tests(root, files)
    frameworks = _frameworks(root)
    profile = {
        "valid_project": True,
        "project_id": project_id_for(root),
        "name": root.name,
        "root": str(root),
        "cwd": str(cwd),
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "languages": [{"name": name, "files": count} for name, count in languages.most_common()],
        "frameworks": frameworks,
        "project_type": _project_type(languages, frameworks, infra, api),
        "infrastructure": infra,
        "api_definitions": api,
        "existing_tests": tests,
        "git": _git(root),
        "scan": {"files_scanned": scanned, "truncated": truncated, "max_files": max_files},
        "notes": [
            "No project source code was copied into KHUNKHAAR.",
            "Environment files were read for variable NAMES only; values never left the project.",
            "No project script, server or test was executed during discovery.",
        ],
    }
    return profile


def write_profile(profile: dict[str, Any]) -> Path:
    folder = paths.project_dir(profile["project_id"])
    target = folder / "project-profile.json"
    target.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o600)

    from core.metadata import store

    store.upsert_project(
        project_id=profile["project_id"],
        name=profile["name"],
        root_path=profile["root"],
        profile_path=str(target),
        languages=[entry["name"] for entry in profile["languages"]],
        frameworks=profile["frameworks"],
        project_type=profile["project_type"],
    )
    return target
