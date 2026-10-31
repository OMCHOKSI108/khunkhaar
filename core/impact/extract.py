"""Dependency extraction from source, schema and configuration files.

Extraction is deliberately conservative. It records what a parser actually saw:

* Python is parsed with the standard `ast` module. Imports, declarations, route decorators, ORM
  model declarations and calls are read from the syntax tree, not guessed from text.
* JavaScript/TypeScript is parsed with tree-sitter when the grammars are installed. Imports,
  classes, exported functions, NestJS/Express routes and calls are read from the syntax tree.
* SQL, Prisma and GraphQL schema files are scanned with narrow, documented patterns because no
  full parser is bundled for them.
* Any other source file becomes a `file` node with `parsed=false`. It is never given edges it was
  not observed to have, and the impact report says the file's dependencies are unknown.

Environment files are recorded by presence only. Their contents are never read, so a secret
cannot reach the graph, an artifact or an AI context through this module.
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.impact import model

try:  # the parsers are optional at runtime; extraction degrades honestly without them
    from tree_sitter import Language, Parser

    import tree_sitter_javascript as _ts_javascript
    import tree_sitter_typescript as _ts_typescript

    _TS_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where the grammars are absent
    _TS_AVAILABLE = False

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "vendor",
    "dist", "build", "target", ".next", ".nuxt", "coverage", ".pytest_cache", ".mypy_cache",
    ".hypothesis", "opt", "evidence", "reports", "data", "runtime", "projects", ".idea",
    ".vscode", "bower_components", ".tox", ".cache",
}

PARSED_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".sql", ".prisma",
                   ".graphql", ".gql"}
# Files that are code but have no extractor here. They still become nodes, so a change to one is
# never silently absent from the impact report.
UNPARSED_SUFFIXES = {".rb", ".php", ".go", ".java", ".kt", ".kts", ".cs", ".rs", ".swift",
                     ".scala", ".c", ".h", ".cc", ".cpp", ".hpp", ".ex", ".exs", ".clj",
                     ".vue", ".svelte", ".lua", ".pl", ".r", ".m", ".mm", ".groovy"}
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "package-lock.json",
    "tsconfig.json", "jsconfig.json", "requirements.txt", "requirements-dev.txt", "Pipfile",
    "poetry.lock", "composer.json", "composer.lock", "Gemfile", "Gemfile.lock", "go.mod",
    "go.sum", "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "Makefile", "alembic.ini", "tox.ini", "mypy.ini", "ruff.toml",
    ".eslintrc", ".eslintrc.json", ".prettierrc",
}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "all", "route",
              "api_route"}
EXPRESS_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
NEST_VERBS = {"Get", "Post", "Put", "Patch", "Delete", "Head", "Options", "All"}
WRITE_CALLS = {
    "add", "add_all", "commit", "delete", "flush", "insert", "merge", "save", "update",
    "upsert", "write", "create", "bulk_create", "bulk_update", "remove", "destroy", "execute",
}
WRITE_SQL = re.compile(r"\b(insert|update|delete|create|alter|drop|truncate)\b", re.I)
WRITE_JS = re.compile(r"\.(save|create|update|delete|insert|upsert|destroy|remove|write|"
                      r"put|patch)\s*\(", re.I)
ORM_BASES = {"Base", "Model", "DeclarativeBase", "AsyncAttrs"}
ORM_EXTERNAL_BASES = (".Model", ".DeclarativeBase", "models.Model", "db.Model")
_JS_HTTP_METHOD = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
                   "delete": "DELETE", "head": "HEAD", "options": "OPTIONS", "all": "ANY"}
_TEST_NAME = re.compile(r"^(?:test_.*\.py|.*_test\.py|.*\.(?:test|spec)\.[cm]?[jt]sx?)$", re.I)

_LITERAL = re.compile(r"""^['"`](.*)['"`]$""")


@dataclass(frozen=True)
class Limits:
    max_files: int = 4000
    max_file_bytes: int = 250_000
    max_symbols_per_file: int = 500
    max_uses_per_file: int = 400
    parse_js_ts: bool = True


@dataclass
class Decl:
    qual: str
    kind: str  # class | function | method | nested
    node_id: str
    line: int
    cls: str | None = None
    parent: str | None = None


@dataclass
class ImportFact:
    module: str
    bindings: list[tuple[str, str]]  # (imported name, local name); imported "" = the module
    level: int
    line: int


@dataclass
class CallFact:
    owner: str
    callee: str
    line: int
    style: str  # name | this | attribute | new


@dataclass
class NameUse:
    owner: str
    name: str
    line: int


@dataclass
class RouteFact:
    method: str
    path: str
    handler: str | None
    evidence: str
    line: int
    confidence: float = 0.95
    origin: str = "explicit"


@dataclass
class ParsedFile:
    rel: str
    node_id: str
    suffix: str
    is_test: bool
    text: str = ""
    decls: dict[str, Decl] = field(default_factory=dict)
    decl_ids: dict[str, Decl] = field(default_factory=dict)
    imports: list[ImportFact] = field(default_factory=list)
    calls: list[CallFact] = field(default_factory=list)
    uses: list[NameUse] = field(default_factory=list)
    routes: list[RouteFact] = field(default_factory=list)
    write_signals: dict[str, bool] = field(default_factory=dict)
    module_names: list[str] = field(default_factory=list)
    router_prefixes: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    entity_map: list[tuple[str, str, str, float, str]] = field(default_factory=list)
    parsed: bool = True
    reason: str = ""

    def decl_for(self, node_id: str) -> Decl | None:
        return self.decl_ids.get(node_id)


@dataclass
class Extraction:
    graph: model.Graph
    languages: dict[str, int]
    parsed_files: int
    unparsed_files: int
    external_imports: int
    unresolved_imports: int
    unresolved_calls: int


def is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    if any(part in ("test", "tests", "__tests__", "spec", "specs") for part in parts[:-1]):
        return True
    return bool(_TEST_NAME.match(parts[-1]))


def _join_paths(prefix: str, path: str) -> str:
    segments = [part.strip("/") for part in (prefix or "", path or "") if part and part.strip("/")]
    return model.normalize_path("/" + "/".join(segments))


def _literal(text: str) -> str | None:
    match = _LITERAL.match((text or "").strip())
    return match.group(1) if match else None


def _read(path: Path, limit: int) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def _walk(root: Path, limits: Limits) -> tuple[list[str], bool]:
    found: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if len(found) >= limits.max_files:
                truncated = True
                break
            suffix = Path(name).suffix.lower()
            if (name in CONFIG_NAMES or name.startswith(".env") or suffix in PARSED_SUFFIXES
                    or suffix in UNPARSED_SUFFIXES):
                found.append(str(Path(dirpath, name).relative_to(root)).replace(os.sep, "/"))
        if truncated:
            break
    return found, truncated


def _python_module_names(rel: str) -> list[str]:
    parts = rel[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    return sorted({".".join(parts[index:]) for index in range(len(parts))})


def _entity_node(graph: model.Graph, name: str, *, source: str, line: int,
                 confidence: float, origin: str, kind: str) -> str:
    node_id = model.entity_id(name)
    graph.add_node(model.Node(node_id, "entity", name, source,
                              {"file": source, "line": line, "declared_as": kind},
                              confidence, origin))
    return node_id


def _api_node(graph: model.Graph, method: str, path: str, *, source: str, line: int,
              confidence: float, origin: str, protocol: str = "http",
              framework: str = "") -> str:
    normalized = model.normalize_path(path)
    node_id = model.api_id(method, normalized)
    graph.add_node(model.Node(node_id, "api", f"{method.upper()} {normalized}", None,
                              {"path": normalized, "method": method.upper(), "source": source,
                               "line": line, "protocol": protocol, "framework": framework},
                              confidence, origin))
    return node_id


def extract(root: Path, project_id: str, limits: Limits | None = None) -> Extraction:
    limits = limits or Limits()
    root = root.resolve()
    graph = model.Graph(project_id=project_id, root=str(root))
    files, truncated = _walk(root, limits)
    graph.files_scanned = len(files)
    graph.truncated = truncated

    node_ids: dict[str, str] = {}
    parsed: list[ParsedFile] = []
    languages: Counter[str] = Counter()
    unparsed: Counter[str] = Counter()
    env_files = 0
    too_large = 0

    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        suffix = Path(rel).suffix.lower()
        if name in CONFIG_NAMES or name.startswith(".env"):
            is_env = name.startswith(".env")
            env_files += 1 if is_env else 0
            node_id = model.config_id(rel)
            graph.add_node(model.Node(node_id, "config", rel, rel,
                                      {"file": rel, "kind": "config", "env": is_env,
                                       "values_read": False}, 1.0))
            node_ids[rel] = node_id
            continue

        is_test = is_test_path(rel)
        node_id = model.test_id(rel) if is_test else model.file_id(rel)
        node_ids[rel] = node_id
        language = _language(suffix)
        languages[language] += 1
        graph.add_node(model.Node(node_id, "test" if is_test else "file", rel, rel,
                                  {"file": rel, "language": language, "kind": "test" if is_test
                                   else "file", "parsed": suffix in PARSED_SUFFIXES}, 1.0))

        if suffix not in PARSED_SUFFIXES:
            unparsed[suffix or name] += 1
            graph.annotate(node_id, parsed=False,
                           reason=f"no extractor is bundled for {suffix or name}")
            continue
        text = _read(root / rel, limits.max_file_bytes)
        if text is None:
            too_large += 1
            graph.annotate(node_id, parsed=False,
                           reason="the file exceeds the size limit and was not parsed")
            continue
        pf = ParsedFile(rel=rel, node_id=node_id, suffix=suffix, is_test=is_test, text=text)
        if suffix == ".py":
            _parse_python(pf, graph, limits)
        elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            _parse_js(pf, graph, limits)
        else:
            _parse_schema(pf, graph)
        if not pf.parsed:
            graph.annotate(node_id, parsed=False, reason=pf.reason)
        parsed.append(pf)

    module_index = _module_index(parsed)
    known = set(node_ids)
    external, unresolved_imports = _resolve_python_imports(parsed, graph, module_index, node_ids)
    unresolved_js = _resolve_js_imports(parsed, graph, known, node_ids)
    unresolved_calls = _resolve_calls(parsed, graph, node_ids)
    _link_entities(parsed, graph)
    _link_routes(parsed, graph)

    if truncated:
        graph.notes.append(
            f"the file scan stopped at {limits.max_files} files; the graph covers only the "
            f"files that were read")
    if unparsed:
        summary = ", ".join(f"{suffix or '<no extension>'}: {count}"
                            for suffix, count in sorted(unparsed.items()))
        graph.notes.append(
            f"{sum(unparsed.values())} source file(s) have no parser here ({summary}); they are "
            f"recorded with parsed=false and their dependencies are unknown, not empty")
    if too_large:
        graph.notes.append(f"{too_large} file(s) exceeded the size limit and were not parsed")
    if env_files:
        graph.notes.append(
            f"{env_files} environment file(s) are recorded by presence only; their contents "
            f"were never read")

    extraction = {
        "languages": dict(sorted(languages.items())),
        "parsed_files": sum(1 for pf in parsed if pf.parsed),
        "unparsed_files": sum(unparsed.values()) + too_large,
        "external_imports": external,
        "unresolved_imports": unresolved_imports + unresolved_js,
        "unresolved_calls": unresolved_calls,
        "environment_files": env_files,
    }
    graph.resolution = {"extraction": extraction}
    return Extraction(graph=graph, languages=dict(sorted(languages.items())),
                      parsed_files=extraction["parsed_files"],
                      unparsed_files=extraction["unparsed_files"],
                      external_imports=external,
                      unresolved_imports=extraction["unresolved_imports"],
                      unresolved_calls=unresolved_calls)


def _language(suffix: str) -> str:
    return {
        ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".sql": "sql",
        ".prisma": "prisma", ".graphql": "graphql", ".gql": "graphql",
    }.get(suffix, suffix.lstrip(".") or "unknown")


# --------------------------------------------------------------------------- Python

class _PyWalker(ast.NodeVisitor):
    def __init__(self, pf: ParsedFile, graph: model.Graph, limits: Limits):
        self.pf = pf
        self.graph = graph
        self.limits = limits
        self.prefix = ""
        self.classes: list[str] = []
        self.symbols: list[str] = []
        self.symbol_count = 0

    # -- module/router setup
    def scan_prefixes(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            target = value = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
                continue
            func = ast.unparse(value.func)
            if func.rsplit(".", 1)[-1] not in ("APIRouter", "Blueprint", "FastAPI", "Router",
                                               "Flask"):
                continue
            prefix = ""
            for keyword in value.keywords:
                if keyword.arg in ("prefix", "url_prefix") and isinstance(keyword.value,
                                                                          ast.Constant) \
                        and isinstance(keyword.value.value, str):
                    prefix = keyword.value.value
            if prefix:
                self.pf.router_prefixes[target.id] = prefix

    # -- declarations
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self.prefix + node.name
        entity = _python_orm(node)
        node_id = model.class_id(self.pf.rel, qual)
        self._add_decl(node_id, qual, "class", node.lineno, None, None)
        if entity is not None:
            name, evidence, confidence, origin = entity
            entity_node = _entity_node(self.graph, name, source=self.pf.rel, line=node.lineno,
                                       confidence=confidence, origin=origin, kind="orm-class")
            self.graph.add_edge(node_id, entity_node, "implements", confidence=confidence,
                                evidence=evidence, origin=origin)
            self.pf.entity_map.append((qual, name, evidence, confidence, origin))
        old_prefix = self.prefix
        self.prefix = qual + "."
        self.classes.append(qual)
        for child in node.body:
            self.visit(child)
        self.classes.pop()
        self.prefix = old_prefix

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = self.prefix + node.name
        enclosing_class = self.classes[-1] if self.classes else None
        if self.symbols:
            kind = "nested"
        elif enclosing_class:
            kind = "method"
        else:
            kind = "function"
        node_id = model.symbol_id(self.pf.rel, qual)
        self._add_decl(node_id, qual, kind, min([node.lineno, *[d.lineno for d in
                                                               node.decorator_list]]),
                       enclosing_class if kind == "method" else None,
                       self.symbols[-1] if self.symbols else None)
        self.pf.write_signals[node_id] = _python_write_signal(node)
        for method, path in _python_routes(node.decorator_list, self.pf.router_prefixes):
            self.pf.routes.append(RouteFact(
                method=method, path=path, handler=qual,
                evidence=f"decorator on {qual} declares this route", line=node.lineno))
        self.symbols.append(node_id)
        old_prefix = self.prefix
        self.prefix = qual + "."
        for child in node.body:
            self.visit(child)
        self.prefix = old_prefix
        self.symbols.pop()

    def _add_decl(self, node_id: str, qual: str, kind: str, line: int,
                  cls: str | None, parent: str | None) -> None:
        self.symbol_count += 1
        if self.symbol_count > self.limits.max_symbols_per_file:
            if self.symbol_count == self.limits.max_symbols_per_file + 1:
                self.graph.notes.append(
                    f"{self.pf.rel} has more declarations than the per-file limit "
                    f"({self.limits.max_symbols_per_file}); only the first ones are in the graph")
            return
        node_type = "class" if kind == "class" else "symbol"
        self.graph.add_node(model.Node(node_id, node_type, qual, self.pf.rel,
                                       {"file": self.pf.rel, "line": line, "kind": kind,
                                        "class": cls, "parent": parent}, 1.0))
        decl = Decl(qual, kind, node_id, line, cls, parent)
        self.pf.decls[qual] = decl
        self.pf.decl_ids[node_id] = decl

    # -- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.pf.imports.append(ImportFact(alias.name, [("", local)], 0, node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        bindings = [(alias.name, alias.asname or alias.name) for alias in node.names]
        self.pf.imports.append(ImportFact(node.module or "", bindings, node.level or 0,
                                          node.lineno))

    # -- calls and name uses
    def visit_Call(self, node: ast.Call) -> None:
        owner = self.symbols[-1] if self.symbols else self.pf.node_id
        func = node.func
        if isinstance(func, ast.Name):
            self._call(owner, func.id, node.lineno, "name")
            self._use(owner, func.id, node.lineno)
        elif isinstance(func, ast.Attribute):
            chain = ast.unparse(func)
            self._use(owner, chain.split(".")[0], node.lineno)
            if chain.startswith(("self.", "cls.")) and chain.count(".") == 1:
                self._call(owner, func.attr, node.lineno, "this")
            elif re.fullmatch(r"[A-Za-z_][\w.]*\.[A-Za-z_]\w*", chain):
                self._call(owner, chain, node.lineno, "attribute")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and self.symbols:
            self._use(self.symbols[-1], node.id, node.lineno)

    def _call(self, owner: str, callee: str, line: int, style: str) -> None:
        if len(self.pf.calls) < self.limits.max_uses_per_file * 8:
            self.pf.calls.append(CallFact(owner, callee, line, style))

    def _use(self, owner: str, name: str, line: int) -> None:
        if len(self.pf.uses) < self.limits.max_uses_per_file:
            self.pf.uses.append(NameUse(owner, name, line))


def _python_orm(node: ast.ClassDef) -> tuple[str, str, float, str] | None:
    bases = [ast.unparse(base) for base in node.bases]
    matched = None
    for base in bases:
        short = base.rsplit(".", 1)[-1]
        if short == "BaseModel":
            continue
        if base in ORM_BASES or short in ORM_BASES or base.endswith(ORM_EXTERNAL_BASES):
            matched = base
            break
    if matched is None:
        return None
    table = None
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__" \
                        and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    table = stmt.value.value
    name = table or node.name
    evidence = f"class {node.name} extends {matched}"
    if table:
        return name, f"{evidence} and declares __tablename__ = {table!r}", 0.85, "explicit"
    if matched in ("Base", "DeclarativeBase") or matched.endswith(ORM_EXTERNAL_BASES):
        return name, f"{evidence}; the table name is not declared, so the class name is used", \
            0.55, "inferred"
    return name, evidence, 0.6, "inferred"


def _python_write_signal(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.attr if isinstance(func, ast.Attribute) else \
                func.id if isinstance(func, ast.Name) else ""
            if name.lower() in WRITE_CALLS:
                return True
        elif isinstance(child, ast.Constant) and isinstance(child.value, str) \
                and WRITE_SQL.search(child.value):
            return True
    return False


def _python_routes(decorators: list[ast.expr], prefixes: dict[str, str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for decorator in decorators:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        verb = decorator.func.attr.lower()
        if verb not in HTTP_VERBS:
            continue
        path = "/"
        if decorator.args and isinstance(decorator.args[0], ast.Constant) \
                and isinstance(decorator.args[0].value, str):
            path = decorator.args[0].value
        elif decorator.args:
            continue
        methods = [verb.upper()]
        if verb in ("route", "api_route"):
            methods = ["GET"]
            for keyword in decorator.keywords:
                if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    methods = [element.value.upper() for element in keyword.value.elts
                               if isinstance(element, ast.Constant)
                               and isinstance(element.value, str)] or ["GET"]
        prefix = prefixes.get(ast.unparse(decorator.func.value), "")
        full = _join_paths(prefix, path)
        for method in methods:
            found.append((method, full))
    return found


def _parse_python(pf: ParsedFile, graph: model.Graph, limits: Limits) -> None:
    try:
        tree = ast.parse(pf.text, filename=pf.rel)
    except (SyntaxError, ValueError, RecursionError) as exc:
        pf.parsed = False
        pf.reason = f"not valid Python 3 ({type(exc).__name__})"
        return
    walker = _PyWalker(pf, graph, limits)
    walker.scan_prefixes(tree)
    for stmt in tree.body:
        walker.visit(stmt)


# --------------------------------------------------------------------------- JavaScript/TypeScript

_PARSERS: dict[str, Any] = {}


def _parser_for(suffix: str) -> Any:
    if not _TS_AVAILABLE:
        return None
    key = "tsx" if suffix == ".tsx" else "ts" if suffix == ".ts" else "js"
    if key not in _PARSERS:
        if key == "js":
            _PARSERS[key] = Parser(Language(_ts_javascript.language()))
        elif key == "tsx":
            _PARSERS[key] = Parser(Language(_ts_typescript.language_tsx()))
        else:
            _PARSERS[key] = Parser(Language(_ts_typescript.language_typescript()))
    return _PARSERS[key]


class _JsWalker:
    def __init__(self, pf: ParsedFile, graph: model.Graph, source: bytes, limits: Limits,
                 routers: set[str], mounts: dict[str, str]):
        self.pf = pf
        self.graph = graph
        self.source = source
        self.limits = limits
        self.routers = routers
        self.mounts = mounts
        self.symbols: list[str] = []
        self.classes: list[str] = []
        self.symbol_count = 0

    def text(self, node: Any) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def name_of(self, node: Any) -> str:
        for child in node.children:
            if child.type in ("identifier", "property_identifier", "private_property_identifier",
                              "type_identifier"):
                return self.text(child)
        return ""

    def decorators(self, node: Any, outer: list[Any] | None = None) -> list[Any]:
        found = list(outer or [])
        found.extend(child for child in node.children if child.type == "decorator")
        return found

    # -- entry
    def walk(self, node: Any) -> None:
        kind = node.type
        if kind == "import_statement":
            self._import(node)
            return
        if kind == "export_statement":
            outer = [child for child in node.children if child.type == "decorator"]
            for child in node.named_children:
                if child.type == "class_declaration":
                    self._class(child, outer)
                elif child.type == "function_declaration":
                    self._function(child)
                elif child.type in ("lexical_declaration", "variable_declaration"):
                    self._variables(child)
                elif child.type == "import_statement":
                    self._import(child, exported=True)
                elif child.type == "call_expression":
                    self._call(child)
            source = node.child_by_field_name("source")
            if source is not None and node.named_children and \
                    all(child.type in ("export_clause",) for child in node.named_children):
                module = _literal(self.text(source))
                if module:
                    self.pf.imports.append(ImportFact(module, [], 0, self._line(node)))
            return
        if kind == "class_declaration":
            self._class(node, None)
            return
        if kind == "function_declaration":
            self._function(node)
            return
        if kind in ("lexical_declaration", "variable_declaration"):
            self._variables(node)
            return
        if kind == "call_expression":
            self._call(node)
            for child in node.named_children:
                self.walk(child)
            return
        if kind == "new_expression":
            self._new(node)
            for child in node.named_children:
                self.walk(child)
            return
        for child in node.named_children:
            self.walk(child)

    def _line(self, node: Any) -> int:
        return int(node.start_point[0]) + 1

    def _add_decl(self, node_id: str, qual: str, kind: str, line: int,
                  cls: str | None, parent: str | None, node_type: str = "symbol",
                  extra: dict[str, Any] | None = None) -> bool:
        self.symbol_count += 1
        if self.symbol_count > self.limits.max_symbols_per_file:
            return False
        meta = {"file": self.pf.rel, "line": line, "kind": kind, "class": cls, "parent": parent}
        meta.update(extra or {})
        self.graph.add_node(model.Node(node_id, node_type, qual, self.pf.rel, meta, 1.0))
        decl = Decl(qual, kind, node_id, line, cls, parent)
        self.pf.decls[qual] = decl
        self.pf.decl_ids[node_id] = decl
        return True

    def _class(self, node: Any, outer: list[Any] | None) -> None:
        name = self.name_of(node)
        if not name:
            return
        decorators = self.decorators(node, outer)
        decorator_names = [_decorator_name(self.text(item)) for item in decorators]
        is_service = "Injectable" in decorator_names
        node_id = model.service_id(self.pf.rel, name) if is_service else \
            model.class_id(self.pf.rel, name)
        line = self._line(node)
        if not self._add_decl(node_id, name, "class", line, None, None,
                              node_type="service" if is_service else "class"):
            return
        controller = ""
        for text in (self.text(item) for item in decorators):
            parsed = _parse_decorator(text)
            if parsed and parsed[0] == "Controller":
                controller = _decorator_path(parsed[1]) or ""
        if "Entity" in decorator_names:
            entity = ""
            for parsed in (_parse_decorator(self.text(item)) for item in decorators):
                if parsed and parsed[0] == "Entity":
                    entity = _decorator_path(parsed[1]) or name
            entity = entity or name
            entity_node = _entity_node(self.graph, entity, source=self.pf.rel, line=line,
                                       confidence=0.85, origin="explicit", kind="typeorm-entity")
            self.graph.add_edge(node_id, entity_node, "implements", confidence=0.85,
                                evidence=f"@Entity decorator on class {name}", origin="explicit")
            self.pf.entity_map.append((name, entity, f"@Entity on class {name}", 0.85,
                                       "explicit"))
        body = node.child_by_field_name("body")
        self.classes.append(name)
        pending: list[Any] = []
        for child in (body.named_children if body is not None else node.named_children):
            if child.type == "decorator":
                # TypeScript attaches a method decorator to the class body, as a sibling that
                # precedes the method; JavaScript nests it inside the method definition.
                pending.append(child)
                continue
            if child.type in ("method_definition", "method_signature"):
                self._method(child, name, controller, pending)
            elif child.type == "class_declaration":
                self._class(child, None)
            elif child.type in ("lexical_declaration", "variable_declaration"):
                self._variables(child)
            else:
                self.walk(child)
            pending = []
        self.classes.pop()

    def _method(self, node: Any, cls: str, controller: str,
                outer: list[Any] | None = None) -> None:
        name = self.name_of(node)
        if not name:
            return
        qual = f"{cls}.{name}"
        node_id = model.symbol_id(self.pf.rel, qual)
        line = self._line(node)
        if not self._add_decl(node_id, qual, "method", line, cls, None):
            return
        self.pf.write_signals[node_id] = bool(WRITE_JS.search(self.text(node)))
        decorators = self.decorators(node, outer)
        for item in decorators:
            parsed = _parse_decorator(self.text(item))
            if not parsed or parsed[0] not in NEST_VERBS:
                continue
            verb = "ANY" if parsed[0] == "All" else parsed[0].upper()
            path = _decorator_path(parsed[1]) or ""
            self.pf.routes.append(RouteFact(
                method=verb, path=_join_paths(controller, path), handler=qual,
                evidence=f"@{parsed[0]} decorator on {qual}", line=line))
        self.symbols.append(node_id)
        for child in node.named_children:
            if child.type != "decorator":
                self.walk(child)
        self.symbols.pop()

    def _function(self, node: Any) -> None:
        name = self.name_of(node)
        if not name:
            return
        node_id = model.symbol_id(self.pf.rel, name)
        line = self._line(node)
        if not self._add_decl(node_id, name, "function", line, None, None):
            return
        self.pf.write_signals[node_id] = bool(WRITE_JS.search(self.text(node)))
        self.symbols.append(node_id)
        for child in node.named_children:
            if child.type != "identifier":
                self.walk(child)
        self.symbols.pop()

    def _variables(self, node: Any) -> None:
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if value is None:
                continue
            if name_node is not None and value.type in ("arrow_function", "function_expression",
                                                        "function"):
                name = self.text(name_node)
                if name_node.type == "object_pattern":
                    continue
                node_id = model.symbol_id(self.pf.rel, name)
                if not self._add_decl(node_id, name, "function", self._line(declarator),
                                      None, None):
                    continue
                self.pf.write_signals[node_id] = bool(WRITE_JS.search(self.text(value)))
                self.symbols.append(node_id)
                for child in value.named_children:
                    self.walk(child)
                self.symbols.pop()
            elif value.type == "call_expression" and name_node is not None:
                func = value.child_by_field_name("function")
                func_text = self.text(func) if func is not None else ""
                arguments = value.child_by_field_name("arguments")
                if func_text in ("require", "require.resolve") and arguments is not None:
                    literal = _string_argument(arguments, 0, self.text)
                    if literal:
                        name = self.text(name_node)
                        if name_node.type == "object_pattern":
                            bindings = [(child.type == "shorthand_property_identifier_pattern"
                                         and self.text(child) or self.text(child), self.text(child))
                                        for child in name_node.named_children]
                            self.pf.imports.append(ImportFact(literal, bindings, 0,
                                                              self._line(value)))
                        else:
                            self.pf.imports.append(ImportFact(literal, [("*", name)], 0,
                                                              self._line(value)))
                else:
                    self.walk(value)
            elif name_node is not None and name_node.type == "identifier":
                # `const router = express.Router()` is a router, not a call worth resolving.
                func = value.child_by_field_name("function") if value.type == "call_expression" \
                    else None
                if func is not None and self.text(func) in ("express.Router", "Router",
                                                            "express"):
                    self.routers.add(self.text(name_node))
                self.walk(value)

    def _import(self, node: Any, exported: bool = False) -> None:
        source = node.child_by_field_name("source")
        module = _literal(self.text(source)) if source is not None else None
        if not module:
            return
        bindings: list[tuple[str, str]] = []
        for child in node.named_children:
            if child.type == "import_clause":
                for item in child.children:
                    if item.type == "identifier":
                        bindings.append(("default", self.text(item)))
                    elif item.type == "namespace_import":
                        ident = self.name_of(item)
                        if ident:
                            bindings.append(("*", ident))
                    elif item.type == "named_imports":
                        for specifier in item.named_children:
                            if specifier.type != "import_specifier":
                                continue
                            imported = specifier.child_by_field_name("name")
                            alias = specifier.child_by_field_name("alias")
                            if imported is not None:
                                bindings.append((self.text(imported),
                                                 self.text(alias) if alias is not None
                                                 else self.text(imported)))
            elif child.type == "export_clause" and exported:
                for specifier in child.named_children:
                    if specifier.type == "export_specifier":
                        imported = specifier.child_by_field_name("name")
                        alias = specifier.child_by_field_name("alias")
                        if imported is not None:
                            bindings.append((self.text(imported),
                                             self.text(alias) if alias is not None
                                             else self.text(imported)))
        self.pf.imports.append(ImportFact(module, bindings, 0, self._line(node)))

    def _call(self, node: Any) -> None:
        func = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if func is None:
            return
        owner = self.symbols[-1] if self.symbols else self.pf.node_id
        if func.type == "identifier":
            name = self.text(func)
            self._record_call(owner, name, self._line(node), "name")
            self._record_use(owner, name, self._line(node))
            return
        if func.type != "member_expression":
            return
        obj = func.child_by_field_name("object")
        prop = func.child_by_field_name("property")
        if obj is None or prop is None:
            return
        obj_text = self.text(obj)
        prop_text = self.text(prop)
        object_name = obj_text if re.fullmatch(r"[A-Za-z_$][\w$]*", obj_text) else ""
        if prop_text.lower() in EXPRESS_VERBS and object_name in self.routers \
                and arguments is not None and obj.type == "identifier":
            path = _string_argument(arguments, 0, self.text)
            if path is not None:
                handler = None
                handler_name = _string_argument(arguments, 1, self.text, identifier=True)
                if handler_name and handler_name in self.pf.decls:
                    handler = handler_name
                prefix = self.mounts.get(obj_text, "")
                self.pf.routes.append(RouteFact(
                    method=("ANY" if prop_text.lower() == "all" else
                            _JS_HTTP_METHOD.get(prop_text.lower(), "ANY")),
                    path=_join_paths(prefix, path), handler=handler,
                    evidence=f"route registration on {obj_text}", line=self._line(node),
                    confidence=0.9 if handler else 0.8))
            self._record_use(owner, object_name, self._line(node))
            return
        if object_name:
            self._record_use(owner, object_name, self._line(node))
        if obj_text in ("this", "super"):
            self._record_call(owner, prop_text, self._line(node), "this")
        elif object_name and object_name not in _JS_NOISE:
            self._record_call(owner, f"{object_name}.{prop_text}", self._line(node), "attribute")

    def _new(self, node: Any) -> None:
        constructor = node.child_by_field_name("constructor")
        if constructor is None or constructor.type != "identifier":
            return
        owner = self.symbols[-1] if self.symbols else self.pf.node_id
        name = self.text(constructor)
        self._record_call(owner, name, self._line(node), "new")
        self._record_use(owner, name, self._line(node))

    def _record_call(self, owner: str, callee: str, line: int, style: str) -> None:
        if len(self.pf.calls) < self.limits.max_uses_per_file * 8:
            self.pf.calls.append(CallFact(owner, callee, line, style))

    def _record_use(self, owner: str, name: str, line: int) -> None:
        if name and len(self.pf.uses) < self.limits.max_uses_per_file:
            self.pf.uses.append(NameUse(owner, name, line))


_JS_NOISE = {"console", "Math", "JSON", "Promise", "Object", "Array", "Date", "Number",
             "String", "Boolean", "process", "window", "document", "module", "require",
             "Reflect", "Symbol", "Map", "Set", "Error", "parseInt", "parseFloat"}


def _decorator_name(text: str) -> str:
    match = re.match(r"@\s*([\w.]+)", (text or "").strip())
    return match.group(1).rsplit(".", 1)[-1] if match else ""


def _parse_decorator(text: str) -> tuple[str, str] | None:
    match = re.match(r"@\s*([\w.]+)\s*(?:\((.*)\))?\s*$", (text or "").strip(), re.S)
    if not match:
        return None
    return match.group(1).rsplit(".", 1)[-1], (match.group(2) or "")


def _decorator_path(arguments: str) -> str:
    argument = (arguments or "").strip()
    literal = _literal(argument)
    if literal is not None:
        return literal
    match = re.search(r"(?:path|url)\s*:\s*['\"`]([^'\"`]*)['\"`]", argument)
    return match.group(1) if match else ""


def _string_argument(arguments: Any, index: int, text_of, *, identifier: bool = False) -> str | None:
    named = [child for child in arguments.named_children
             if child.type not in ("comment",)]
    if index >= len(named):
        return None
    node = named[index]
    if identifier:
        return text_of(node) if node.type == "identifier" else None
    if node.type in ("string", "template_string"):
        return _literal(text_of(node))
    return None


def _js_routers(root: Any, text_of) -> tuple[set[str], dict[str, str]]:
    # Conventional names (`app`, `router`, ...) are only trusted when they were declared from an
    # Express factory. Otherwise `api.get('/x')` on an HTTP client would be recorded as a route.
    routers: set[str] = set()
    mounts: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is not None and value is not None and value.type == "call_expression":
                func = value.child_by_field_name("function")
                if func is not None and text_of(func) in ("express.Router", "Router", "express"):
                    routers.add(text_of(name))
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "member_expression":
                prop = func.child_by_field_name("property")
                if prop is not None and text_of(prop) == "use":
                    arguments = node.child_by_field_name("arguments")
                    if arguments is not None:
                        named = [child for child in arguments.named_children]
                        if len(named) >= 2 and named[1].type == "identifier":
                            prefix = text_of(named[0])
                            literal = _literal(prefix) if named[0].type in (
                                "string", "template_string") else None
                            if literal:
                                mounts[text_of(named[1])] = literal
        stack.extend(node.named_children)
    return routers, mounts


def _parse_js(pf: ParsedFile, graph: model.Graph, limits: Limits) -> None:
    parser = _parser_for(pf.suffix) if limits.parse_js_ts else None
    if parser is None:
        pf.parsed = False
        pf.reason = ("the JavaScript/TypeScript parser is not installed or was disabled; the "
                     "file's dependencies are unknown, not empty")
        return
    source = pf.text.encode("utf-8", "replace")
    tree = parser.parse(source)
    routers, mounts = _js_routers(tree.root_node,
                                  lambda node: source[node.start_byte:node.end_byte]
                                  .decode("utf-8", "replace"))
    walker = _JsWalker(pf, graph, source, limits, routers, mounts)
    walker.walk(tree.root_node)


# --------------------------------------------------------------------------- schema files

_SQL_TABLE = re.compile(r"(?im)^\s*(?:create|alter)\s+table\s+(?:if\s+not\s+exists\s+)?"
                        r"[\"`\[]?([A-Za-z_][\w.]*)[\"`\]]?")
_PRISMA_MODEL = re.compile(r"(?im)^\s*model\s+(\w+)\s*\{([^}]*)\}")
_GRAPHQL_TYPE = re.compile(r"(?im)^\s*type\s+(Query|Mutation)\s*\{(.*?)^\s*\}", re.S)


def _parse_schema(pf: ParsedFile, graph: model.Graph) -> None:
    text = pf.text
    rel = pf.rel
    if pf.suffix == ".sql":
        for match in _SQL_TABLE.finditer(text):
            name = match.group(1).split(".")[-1]
            line = text[:match.start()].count("\n") + 1
            node_id = _entity_node(graph, name, source=rel, line=line, confidence=0.9,
                                   origin="explicit", kind="sql-table")
            graph.add_edge(pf.node_id, node_id, "writes", confidence=0.9,
                           evidence=f"schema statement declares table {name}")
    elif pf.suffix == ".prisma":
        for match in _PRISMA_MODEL.finditer(text):
            model_name, body = match.group(1), match.group(2)
            mapped = re.search(r"@@map\(\s*\"([^\"]+)\"\s*\)", body)
            name = mapped.group(1) if mapped else model_name
            line = text[:match.start()].count("\n") + 1
            node_id = _entity_node(graph, name, source=rel, line=line, confidence=0.9,
                                   origin="explicit", kind="prisma-model")
            graph.add_edge(pf.node_id, node_id, "writes", confidence=0.9,
                           evidence=f"Prisma model {model_name} declares this table")
    elif pf.suffix in (".graphql", ".gql"):
        for match in _GRAPHQL_TYPE.finditer(text):
            operation = match.group(1).upper()
            body = match.group(2)
            for field_match in re.finditer(r"(?m)^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*[\[\]!\w]+",
                                           body):
                field = field_match.group(1)
                line = text[:match.start(2)].count("\n") + body[:field_match.start()].count("\n")
                _api_node(graph, operation, f"/graphql:{field}", source=rel, line=line,
                          confidence=0.8, origin="explicit", protocol="graphql")
        graph.notes.append(
            f"{rel} declares GraphQL operations; they are recorded as api nodes, but resolvers "
            f"are not linked by the current extractor")


# --------------------------------------------------------------------------- imports

def _module_index(parsed: list[ParsedFile]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for pf in parsed:
        if pf.suffix != ".py" or not pf.parsed:
            continue
        pf.module_names = _python_module_names(pf.rel)
        for name in pf.module_names:
            index[name].append(pf.rel)
    return {name: sorted(set(paths)) for name, paths in index.items()}


def _lookup_module(index: dict[str, list[str]], name: str) -> str | None:
    candidates = index.get(name) or []
    return candidates[0] if len(candidates) == 1 else None


def _resolve_python_imports(parsed: list[ParsedFile], graph: model.Graph,
                            module_index: dict[str, list[str]], node_ids: dict[str, str]
                            ) -> tuple[int, int]:
    external = 0
    unresolved = 0
    by_rel = {pf.rel: pf for pf in parsed}

    def link(pf: ParsedFile, target_rel: str, evidence: str) -> str:
        target_node = node_ids[target_rel]
        graph.add_edge(pf.node_id, target_node, "imports", evidence=evidence, confidence=1.0)
        if pf.is_test:
            graph.add_edge(pf.node_id, target_node, "tests",
                           evidence=f"test module {evidence}", confidence=0.85)
        return target_node

    for pf in parsed:
        if pf.suffix != ".py" or not pf.parsed:
            continue
        for fact in pf.imports:
            base = fact.module
            if fact.level:
                package = pf.rel.split("/")[:-1]
                drop = fact.level - 1
                if drop:
                    package = package[:-drop] if drop <= len(package) else []
                base = ".".join([*package, fact.module]) if fact.module else ".".join(package)
            target_rel = _lookup_module(module_index, base) if base else None
            if target_rel is not None:
                target_node = link(pf, target_rel, f"imports {base}")
                if base:
                    pf.aliases.setdefault(base, target_node)
                    if "." in base:
                        pf.aliases.setdefault(base.split(".")[0], target_node)
                for imported, local in fact.bindings:
                    if not imported:
                        pf.aliases.setdefault(local, target_node)
                        continue
                    submodule = _lookup_module(module_index,
                                               f"{base}.{imported}" if base else imported)
                    if submodule is not None:
                        pf.aliases.setdefault(local, node_ids[submodule])
                        continue
                    target_pf = by_rel.get(target_rel)
                    decl = target_pf.decls.get(imported) if target_pf else None
                    pf.aliases.setdefault(local, decl.node_id if decl else target_node)
                continue

            # `from . import models` and `from package import models` name a submodule that
            # exists even when the package itself has no `__init__.py` in the scan.
            linked = False
            for imported, local in fact.bindings:
                if not imported:
                    continue
                submodule = _lookup_module(module_index,
                                           f"{base}.{imported}" if base else imported)
                if submodule is None:
                    continue
                node = link(pf, submodule, f"imports {base + '.' if base else ''}{imported}")
                pf.aliases.setdefault(local, node)
                linked = True
            if linked:
                continue
            if fact.level or (base and base in module_index):
                unresolved += 1
            else:
                external += 1
    return external, unresolved


def _resolve_js_imports(parsed: list[ParsedFile], graph: model.Graph, known: set[str],
                        node_ids: dict[str, str]) -> int:
    unresolved = 0
    by_rel = {pf.rel: pf for pf in parsed}
    for pf in parsed:
        if pf.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs") or not pf.parsed:
            continue
        for fact in pf.imports:
            target_rel = _resolve_js_module(pf.rel, fact.module, known)
            if target_rel is None:
                if fact.module.startswith("."):
                    unresolved += 1
                continue
            target_node = node_ids[target_rel]
            graph.add_edge(pf.node_id, target_node, "imports",
                           evidence=f"import {fact.module}", confidence=1.0)
            if pf.is_test:
                graph.add_edge(pf.node_id, target_node, "tests",
                               evidence=f"test module imports {fact.module}", confidence=0.85)
            target_pf = by_rel.get(target_rel)
            for imported, local in fact.bindings:
                if imported in ("*", "default"):
                    pf.aliases.setdefault(local, target_node)
                else:
                    decl = target_pf.decls.get(imported) if target_pf else None
                    pf.aliases.setdefault(local, decl.node_id if decl else target_node)
    return unresolved


_JS_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_js_module(rel: str, spec: str, known: set[str]) -> str | None:
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
    elif spec.startswith(("@/", "~/")):
        base = spec[2:]
    else:
        return None
    if base.startswith(".."):
        return None
    candidates: list[str] = []
    root, ext = os.path.splitext(base)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        candidates.extend([root + ".ts", root + ".tsx"])
    candidates.append(base)
    candidates.extend(base + suffix for suffix in _JS_EXTENSIONS if suffix)
    for index_name in ("index.ts", "index.tsx", "index.js", "index.jsx"):
        candidates.append(f"{base.rstrip('/')}/{index_name}")
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


# --------------------------------------------------------------------------- calls

def _resolve_calls(parsed: list[ParsedFile], graph: model.Graph,
                   node_ids: dict[str, str]) -> int:
    unresolved = 0
    by_rel = {pf.rel: pf for pf in parsed}
    for pf in parsed:
        if not pf.parsed:
            continue
        aliases = pf.aliases
        for call in pf.calls:
            target = _resolve_call_target(pf, call, aliases, by_rel)
            if target is None:
                unresolved += 1
                continue
            if target == call.owner:
                continue
            confidence = 0.9
            evidence = f"{call.callee}() at line {call.line}"
            target_node = graph.node(target)
            if target_node is not None and target_node.type in ("file", "test", "config"):
                confidence = 0.7
                evidence += " (resolved only to the module, not the symbol)"
            graph.add_edge(call.owner, target, "calls", evidence=evidence, confidence=confidence)
            if pf.is_test:
                graph.add_edge(pf.node_id, target, "tests",
                               evidence=f"test calls {call.callee}() at line {call.line}",
                               confidence=0.8)
    return unresolved


def _resolve_call_target(pf: ParsedFile, call: CallFact, aliases: dict[str, str],
                         by_rel: dict[str, ParsedFile]) -> str | None:
    if call.style == "this":
        owner_decl = pf.decl_for(call.owner)
        if owner_decl and owner_decl.cls:
            decl = pf.decls.get(f"{owner_decl.cls}.{call.callee}")
            return decl.node_id if decl else None
        return None
    if call.style in ("name", "new"):
        if call.callee in aliases:
            return aliases[call.callee]
        decl = pf.decls.get(call.callee)
        if decl:
            return decl.node_id
        nested = [item for item in pf.decls.values()
                  if item.qual.endswith("." + call.callee)]
        return nested[0].node_id if len(nested) == 1 else None
    # attribute: `namespace.symbol` or `a.b.symbol`
    parts = call.callee.split(".")
    for index in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:index])
        base = aliases.get(prefix)
        if not base:
            continue
        attr = parts[index]
        target_rel = model.path_of(base)
        target_pf = by_rel.get(target_rel or "")
        decl = target_pf.decls.get(attr) if target_pf else None
        if decl is not None:
            return decl.node_id
        if base.startswith(("file:", "test:", "config:")):
            return base
        return None
    return None


# --------------------------------------------------------------------------- linking

def _link_entities(parsed: list[ParsedFile], graph: model.Graph) -> None:
    entity_classes: dict[str, str] = {}
    for pf in parsed:
        for qual, entity, _evidence, _confidence, _origin in pf.entity_map:
            entity_classes[qual.rsplit(".", 1)[-1]] = model.entity_id(entity)
    if not entity_classes:
        return
    for pf in parsed:
        if not pf.parsed:
            continue
        for use in pf.uses:
            entity = entity_classes.get(use.name)
            if entity is None or not graph.has(entity):
                continue
            relation = "writes" if pf.write_signals.get(use.owner) else "reads"
            decl = pf.decl_for(use.owner)
            label = decl.qual if decl else use.owner
            graph.add_edge(use.owner, entity, relation, confidence=0.6, origin="inferred",
                           evidence=f"{label} references ORM model {use.name} at line {use.line}")


def _link_routes(parsed: list[ParsedFile], graph: model.Graph) -> None:
    for pf in parsed:
        for route in pf.routes:
            api = _api_node(graph, route.method, route.path, source=pf.rel, line=route.line,
                            confidence=route.confidence, origin=route.origin,
                            framework=_framework_for_suffix(pf.suffix))
            graph.add_edge(pf.node_id, api, "exposes", confidence=0.8,
                           evidence=route.evidence)
            if route.handler:
                decl = pf.decls.get(route.handler)
                if decl is not None:
                    graph.add_edge(decl.node_id, api, "exposes", confidence=route.confidence,
                                   evidence=route.evidence)


def _framework_for_suffix(suffix: str) -> str:
    return {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
            ".tsx": "tsx", ".mjs": "javascript", ".cjs": "javascript"}.get(suffix, "")
