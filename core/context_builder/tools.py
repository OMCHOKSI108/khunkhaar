"""Context Tool Gateway: exposes agent-callable tools for codebase retrieval."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.context_builder import (
    ast_analyzer,
    doc_generator,
    graph_store,
    hybrid_search,
    scanner,
)
from core.context_builder.assembler import assemble_context as _assemble
from core.context_builder.models import AssembledContext, CodeRelationship, CodeSymbol, SearchResult
from core.server import path_resolver, store, vector_store


def build_full_project_context(project_id: str, raw_codebase_path: str) -> dict[str, Any]:
    root = path_resolver.resolve_codebase_path(raw_codebase_path)
    if not root or not root.exists():
        return {"error": f"Invalid codebase path: {raw_codebase_path}"}

    # 1. Scan directory & frameworks
    files = scanner.scan_directory(root)
    fw_info = scanner.detect_frameworks_and_deps(root)

    # 2. Extract AST symbols and relationships
    all_symbols: list[CodeSymbol] = []
    all_rels: list[CodeRelationship] = []
    for f in files:
        syms, rels = ast_analyzer.analyze_file(project_id, Path(f.path), f.relative_path)
        all_symbols.extend(syms)
        all_rels.extend(rels)

    # 3. Store Graph
    graph_store.save_symbols(project_id, all_symbols)
    graph_store.save_relationships(project_id, all_rels)

    # 4. Ingest Vectors
    vector_store.ingest_codebase_to_vectors(project_id, root)

    # 5. Generate Markdown Documentation
    proj = store.get_project(project_id)
    pname = proj.name if proj else "Project"
    doc_p = doc_generator.generate_project_md(pname, files, fw_info)
    doc_api = doc_generator.generate_api_md(pname, all_symbols)
    doc_db = doc_generator.generate_database_md(pname, store.get_project_auth_config(project_id))
    doc_generator.write_context_directory(root, [doc_p, doc_api, doc_db])

    return {
        "status": "success", "files_indexed": len(files),
        "symbols_extracted": len(all_symbols), "relationships_recorded": len(all_rels),
        "frameworks": fw_info.get("frameworks", []),
    }


def search_codebase(project_id: str, query: str, top_k: int = 10) -> list[dict[str, Any]]:
    results = hybrid_search.hybrid_search(project_id, query, top_k=top_k)
    return [{
        "file_path": r.file_path, "start_line": r.start_line, "end_line": r.end_line,
        "content": r.content, "score": round(r.score, 4), "match_type": r.match_type,
    } for r in results]


def find_symbol(project_id: str, symbol_name: str) -> list[dict[str, Any]]:
    return graph_store.find_symbol(project_id, symbol_name)


def get_symbol_references(project_id: str, symbol_name: str) -> list[dict[str, Any]]:
    return graph_store.get_symbol_references(project_id, symbol_name)


def trace_execution_flow(project_id: str, entry_symbol: str, max_depth: int = 3) -> list[dict[str, Any]]:
    return graph_store.trace_execution_flow(project_id, entry_symbol, max_depth)


def get_file_content(project_id: str, file_path: str, start_line: int = 1, end_line: int = 100) -> str:
    proj = store.get_project(project_id)
    root = path_resolver.resolve_codebase_path(proj.codebase_dir if proj else "")
    if not root:
        return "Error: Project codebase not found"
    target = root / file_path
    if not target.exists():
        return f"Error: File '{file_path}' does not exist"
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        slice_lines = lines[max(0, start_line - 1):end_line]
        return "\n".join(f"{start_line + i}: {l}" for i, l in enumerate(slice_lines))
    except Exception as exc:
        return f"Error reading file: {exc}"


def assemble_context(project_id: str, task: str, include: list[str] | None = None, token_budget: int = 12000) -> dict[str, Any]:
    ctx = _assemble(project_id, task, include=include, token_budget=token_budget)
    return {
        "project_id": ctx.project_id, "task": ctx.task, "project_summary": ctx.project_summary,
        "relevant_files": ctx.relevant_files, "relevant_symbols": ctx.relevant_symbols,
        "relationships": ctx.relationships, "database_schema": ctx.database_schema,
        "code_snippets": ctx.code_snippets, "context_tokens": ctx.context_tokens,
        "missing_information": ctx.missing_information,
    }
