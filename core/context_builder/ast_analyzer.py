"""Structural Code Analysis: extracts symbols, routes, models, and relationships."""
from __future__ import annotations

import ast
import re
from pathlib import Path
import uuid
from typing import Any

from core.context_builder.models import CodeRelationship, CodeSymbol


def parse_python_file(project_id: str, file_path: Path, rel_path: str) -> tuple[list[CodeSymbol], list[CodeRelationship]]:
    symbols: list[CodeSymbol] = []
    rels: list[CodeRelationship] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return symbols, rels

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s_id = str(uuid.uuid4())[:8]
            doc = ast.get_docstring(node) or ""
            sig = f"def {node.name}(...)"
            symbols.append(CodeSymbol(s_id, project_id, node.name, "function", rel_path,
                                      node.lineno, getattr(node, "end_lineno", node.lineno), sig, doc))
        elif isinstance(node, ast.ClassDef):
            s_id = str(uuid.uuid4())[:8]
            symbols.append(CodeSymbol(s_id, project_id, node.name, "class", rel_path,
                                      node.lineno, getattr(node, "end_lineno", node.lineno), f"class {node.name}"))
            for base in node.bases:
                if isinstance(base, ast.Name):
                    rels.append(CodeRelationship(str(uuid.uuid4())[:8], project_id, node.name, base.id, "EXTENDS", rel_path))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            for alias in node.names:
                rels.append(CodeRelationship(str(uuid.uuid4())[:8], project_id, rel_path, mod or alias.name, "IMPORTS", rel_path))
    return symbols, rels


JS_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(", re.M)
JS_CONST_FUNC_RE = re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", re.M)
JS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)(?:\s+extends\s+([A-Za-z0-9_$]+))?", re.M)
JS_ROUTE_RE = re.compile(r"(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
JS_IMPORT_RE = re.compile(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\)")


JS_METHOD_RE = re.compile(r"^\s*(?:async\s+)?([A-Za-z0-9_$]+)\s*\([^)]*\)\s*(?::\s*[^{]+)?\s*\{")
JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return"}


def parse_js_ts_file(project_id: str, file_path: Path, rel_path: str) -> tuple[list[CodeSymbol], list[CodeRelationship]]:
    symbols: list[CodeSymbol] = []
    rels: list[CodeRelationship] = []
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return symbols, rels

    for i, line in enumerate(lines, 1):
        m_func = JS_FUNC_RE.search(line) or JS_CONST_FUNC_RE.search(line)
        m_meth = JS_METHOD_RE.search(line) if not m_func else None
        if m_func:
            symbols.append(CodeSymbol(str(uuid.uuid4())[:8], project_id, m_func.group(1), "function",
                                      rel_path, i, min(i + 40, len(lines)), line.strip()))
        elif m_meth and m_meth.group(1) not in JS_KEYWORDS:
            symbols.append(CodeSymbol(str(uuid.uuid4())[:8], project_id, m_meth.group(1), "method",
                                      rel_path, i, min(i + 40, len(lines)), line.strip()))
        m_cls = JS_CLASS_RE.search(line)
        if m_cls:
            cls_name, base_name = m_cls.group(1), m_cls.group(2)
            symbols.append(CodeSymbol(str(uuid.uuid4())[:8], project_id, cls_name, "class",
                                      rel_path, i, min(i + 80, len(lines)), line.strip()))
            if base_name:
                rels.append(CodeRelationship(str(uuid.uuid4())[:8], project_id, cls_name, base_name, "EXTENDS", rel_path))
        m_route = JS_ROUTE_RE.search(line)
        if m_route:
            verb, path = m_route.group(1).upper(), m_route.group(2)
            symbols.append(CodeSymbol(str(uuid.uuid4())[:8], project_id, f"{verb} {path}", "route",
                                      rel_path, i, i + 5, f"{verb} {path}"))
        m_imp = JS_IMPORT_RE.search(line)
        if m_imp:
            target = m_imp.group(1) or m_imp.group(2) or ""
            rels.append(CodeRelationship(str(uuid.uuid4())[:8], project_id, rel_path, target, "IMPORTS", rel_path))
    return symbols, rels


def analyze_file(project_id: str, file_path: Path, rel_path: str) -> tuple[list[CodeSymbol], list[CodeRelationship]]:
    ext = file_path.suffix.lower()
    if ext == ".py":
        return parse_python_file(project_id, file_path, rel_path)
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        return parse_js_ts_file(project_id, file_path, rel_path)
    return [], []
