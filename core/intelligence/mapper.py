"""Static application intelligence.

Builds a bounded map of an unfamiliar repository: routes, API endpoints, data entities, auth
signals, existing tests/specs and documented business rules. Everything is derived from files
with an explicit path/line reference; nothing is presented as a fact without evidence.

This is intentionally language-tolerant (regex + structure heuristics) rather than a full
parser per framework. It is used to generate missions, not to prove correctness.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "vendor",
    "dist", "build", "target", ".next", ".nuxt", "coverage", ".pytest_cache", ".mypy_cache",
    ".hypothesis", "opt", "evidence", "reports", "data", "runtime", "projects",
}
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".php", ".rb", ".go", ".java", ".cs",
                 ".kt", ".ex", ".exs", ".sql", ".prisma"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}
MAX_FILES = 6000
MAX_FILE_BYTES = 250_000

ROUTE_PATTERNS = [
    re.compile(r"@(?:app|router|bp|blueprint)\.(get|post|put|patch|delete|head|options|route)\(\s*"
               r"[\"']([^\"']+)[\"']", re.I),
    re.compile(r"\b(?:app|router|server|api)\.(get|post|put|patch|delete|head|options)\(\s*"
               r"[\"'`]([^\"'`]+)[\"'`]", re.I),
    re.compile(r"Route::(get|post|put|patch|delete|any|match)\(\s*[\"']([^\"']+)[\"']", re.I),
    re.compile(r"\bpath\(\s*[\"']([^\"']+)[\"']\s*,", re.I),
    re.compile(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*)?[\"']([^\"']+)[\"']",
               re.I),
]
MODEL_PATTERNS = [
    re.compile(r"^class\s+(\w+)\s*\([^)]*(?:Base|Model)[^)]*\)", re.M),
    re.compile(r"__tablename__\s*=\s*[\"'](\w+)[\"']"),
    re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)[\"`]?", re.I),
    re.compile(r"^\s*model\s+(\w+)\s*\{", re.M),
    re.compile(r"^\s*class\s+(\w+)\s*:\s*.*\bEntity\b", re.M),
]
AUTH_PATTERN = re.compile(
    r"\b(jwt|oauth|session|login|logout|authenticate|authorization|permission|role|"
    r"csrf|current_user|require_auth|is_admin|Depends\s*\(\s*get_current)", re.I)
RULE_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:RULE\s+)?(?P<id>R\d{1,3})\s*[:\-–]\s*(?P<text>.+?)\s*$")
BARE_RULE_PATTERN = re.compile(
    r"(?im)^\s*[-*]\s*(?P<text>(?:must|shall|should|may only|never|cannot|requires?)\b.+?)\s*$")
HEADING_PATTERN = re.compile(r"(?im)^\s*(?:#{1,6}\s*)?(?P<title>business rules?|requirements?|rules?)\s*:?\s*$")


@dataclass
class Endpoint:
    method: str
    path: str
    source: str
    line: int
    framework: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method.upper(), "path": self.path, "source": self.source,
                "line": self.line, "framework": self.framework}


@dataclass
class Rule:
    rule_id: str
    text: str
    source: str
    line: int
    kind: str = "other"
    confidence: float = 0.9
    origin: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "text": self.text, "source": self.source,
                "line": self.line, "kind": self.kind, "confidence": self.confidence,
                "origin": self.origin}


@dataclass
class ApplicationMap:
    project_id: str
    root: str
    endpoints: list[Endpoint] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    auth_signals: list[str] = field(default_factory=list)
    api_specs: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False

    @property
    def features(self) -> list[str]:
        names: set[str] = set()
        for endpoint in self.endpoints:
            parts = [p for p in endpoint.path.split("/") if p and not p.startswith("{")
                     and not p.startswith(":") and not p.startswith("<")]
            if not parts:
                continue
            head = parts[0]
            if head in ("api", "v1", "v2", "v3") and len(parts) > 1:
                head = parts[1]
            if not head.startswith("$") and len(head) <= 40:
                names.add(head.lower())
        return sorted(names)

    def endpoints_for_feature(self, feature: str) -> list[Endpoint]:
        feature = feature.lower()
        return [e for e in self.endpoints if feature in e.path.lower()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root": self.root,
            "frameworks": self.frameworks,
            "features": self.features,
            "endpoints": [e.to_dict() for e in self.endpoints],
            "entities": self.entities,
            "auth_signals": self.auth_signals,
            "api_specs": self.api_specs,
            "docs": self.docs,
            "tests": self.tests,
            "rules": [r.to_dict() for r in self.rules],
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }


def _iter_files(root: Path) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for path in root.rglob("*"):
        if len(files) >= MAX_FILES:
            truncated = True
            break
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        if path.suffix.lower() not in CODE_SUFFIXES | DOC_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files, truncated


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _framework_for(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        if "FastAPI(" in text or "from fastapi" in text:
            return "fastapi"
        if "Flask(" in text or "from flask" in text:
            return "flask"
        if "django" in text.lower():
            return "django"
    if suffix in (".js", ".ts", ".tsx", ".jsx"):
        if "express" in text or "app.get(" in text:
            return "express"
        if "nestjs" in text.lower() or "@Controller" in text:
            return "nestjs"
    if suffix == ".php":
        return "laravel"
    return ""


def _parse_openapi(path: Path) -> list[Endpoint]:
    text = _read(path)
    try:
        doc = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(doc, dict) or "paths" not in doc:
        return []
    found: list[Endpoint] = []
    for route, methods in (doc.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method in methods:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                found.append(Endpoint(method.upper(), str(route), str(path.name), 0, "openapi"))
    return found


def build(root: Path, project_id: str, rules_classifier=None) -> ApplicationMap:
    root = root.resolve()
    app = ApplicationMap(project_id=project_id, root=str(root))
    files, truncated = _iter_files(root)
    app.truncated = truncated
    seen: set[tuple[str, str]] = set()

    for path in files:
        rel = str(path.relative_to(root))
        suffix = path.suffix.lower()
        if suffix in DOC_SUFFIXES:
            app.docs.append(rel)
            if rules_classifier is not None:
                app.rules.extend(rules_classifier(rel, _read(path)))
            continue
        text = _read(path)
        app.files_scanned += 1
        framework = _framework_for(path, text)
        if framework and framework not in app.frameworks:
            app.frameworks.append(framework)
        if AUTH_PATTERN.search(text):
            app.auth_signals.append(rel)
        if path.name.lower().startswith("test") or "tests" in path.parts or "spec" in path.name.lower():
            app.tests.append(rel)
        if path.name.lower() in ("openapi.json", "openapi.yaml", "swagger.json", "swagger.yaml"):
            app.api_specs.append(rel)
            for endpoint in _parse_openapi(path):
                app.endpoints.append(endpoint)
            continue

        lines = text.splitlines()
        for pattern in ROUTE_PATTERNS:
            for match in pattern.finditer(text):
                groups = match.groups()
                if len(groups) == 2:
                    verb, route = groups[0].lower(), groups[1]
                else:
                    verb, route = "any", groups[0]
                if verb == "route":
                    verb = "any"
                key = (verb.upper(), route)
                if key in seen:
                    continue
                seen.add(key)
                line_no = text[:match.start()].count("\n") + 1
                app.endpoints.append(Endpoint(verb.upper(), route, rel, line_no, framework))
        for pattern in MODEL_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1)
                if name in ("Base", "Model"):
                    continue
                line_no = text[:match.start()].count("\n") + 1
                entity = {"name": name, "source": rel, "line": line_no}
                if entity not in app.entities:
                    app.entities.append(entity)
        if rules_classifier is not None:
            app.rules.extend(rules_classifier(rel, text))
    app.entities.sort(key=lambda item: item["name"].lower())
    app.endpoints.sort(key=lambda item: (item.path, item.method))
    return app
