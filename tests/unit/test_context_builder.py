"""Unit tests for Tool Context Builder (TCB)."""
from pathlib import Path
import pytest

from core.context_builder import ast_analyzer, benchmark, doc_generator, graph_store, scanner, tools
from core.context_builder.models import CodeRelationship, CodeSymbol, FileInfo


def test_scanner_directory(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("console.log('hello');")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.js").write_text("bad")
    (tmp_path / "package.json").write_text('{"dependencies": {"express": "^4.18.2"}}')

    files = scanner.scan_directory(tmp_path)
    assert len(files) == 2  # index.ts and package.json (node_modules excluded)
    assert any(f.relative_path == "src/index.ts" for f in files)

    fw = scanner.detect_frameworks_and_deps(tmp_path)
    assert "Express" in fw["frameworks"]


def test_ast_python_analysis(tmp_path: Path):
    py_file = tmp_path / "service.py"
    py_file.write_text("import os\n\ndef calculate_tax(amount: float):\n    return amount * 0.2\n")
    syms, rels = ast_analyzer.analyze_file("p1", py_file, "service.py")
    assert any(s.symbol_name == "calculate_tax" and s.symbol_type == "function" for s in syms)
    assert any(r.relationship_type == "IMPORTS" and "os" in r.target_symbol for r in rels)


def test_ast_js_ts_analysis(tmp_path: Path):
    ts_file = tmp_path / "router.ts"
    ts_file.write_text("import express from 'express';\nrouter.get('/api/users', getUsers);\nfunction getUsers() {}\n")
    syms, rels = ast_analyzer.analyze_file("p1", ts_file, "router.ts")
    assert any(s.symbol_type == "route" and "GET /api/users" in s.symbol_name for s in syms)
    assert any(s.symbol_name == "getUsers" and s.symbol_type == "function" for s in syms)


def test_graph_store_and_flow():
    syms = [
        CodeSymbol("s1", "proj_test", "authMiddleware", "function", "auth.ts", 10, 30),
        CodeSymbol("s2", "proj_test", "loginHandler", "function", "login.ts", 5, 25),
    ]
    rels = [
        CodeRelationship("r1", "proj_test", "loginHandler", "authMiddleware", "CALLS", "login.ts")
    ]
    graph_store.save_symbols("proj_test", syms)
    graph_store.save_relationships("proj_test", rels)

    found = graph_store.find_symbol("proj_test", "auth")
    assert len(found) >= 1
    assert found[0]["symbol_name"] == "authMiddleware"

    flow = graph_store.trace_execution_flow("proj_test", "loginHandler")
    assert len(flow) >= 1
    assert flow[0]["target_symbol"] == "authMiddleware"


def test_doc_generator():
    files = [FileInfo("src/main.ts", "src/main.ts", 100, "TypeScript", is_entrypoint=True)]
    doc = doc_generator.generate_project_md("MyProject", files, {"frameworks": ["NestJS"]})
    assert "# MyProject — Project Context" in doc.content
    assert "NestJS" in doc.content


def test_benchmark_evaluator():
    queries = [
        {"query": "auth token", "expected_files": ["auth.ts"]},
    ]
    report = benchmark.run_accuracy_benchmark("proj_test", queries)
    assert "metrics" in report
    assert "recall_at_1" in report["metrics"]
