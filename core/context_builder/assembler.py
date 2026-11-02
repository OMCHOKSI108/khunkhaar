"""Context Assembler: builds task-tailored context packages within token budgets."""
from __future__ import annotations

import json
from typing import Any

from core.context_builder import graph_store, hybrid_search
from core.context_builder.models import AssembledContext
from core.server import store


def assemble_context(
    project_id: str,
    task: str,
    include: list[str] | None = None,
    token_budget: int = 12000,
) -> AssembledContext:
    inc = set(include or ["source_code", "database_schema", "symbols"])
    char_budget = token_budget * 4
    used_chars = 0

    proj = store.get_project(project_id)
    summary = f"Project: {proj.name if proj else project_id} (Target: {proj.target_url if proj else 'N/A'})"
    used_chars += len(summary)

    relevant_files: list[str] = []
    relevant_symbols: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    code_snippets: list[dict[str, Any]] = []
    db_schema: list[dict[str, Any]] = []

    # 1. Hybrid Search for task
    if "source_code" in inc or "symbols" in inc:
        hits = hybrid_search.hybrid_search(project_id, task, top_k=8)
        for h in hits:
            if h.file_path not in relevant_files:
                relevant_files.append(h.file_path)
            snippet_str = f"File: {h.file_path} (Lines {h.start_line}-{h.end_line})\n{h.content}\n"
            if used_chars + len(snippet_str) <= char_budget:
                code_snippets.append({
                    "file_path": h.file_path,
                    "start_line": h.start_line,
                    "end_line": h.end_line,
                    "content": h.content,
                    "score": round(h.score, 4),
                    "match_type": h.match_type,
                })
                used_chars += len(snippet_str)

    # 2. Extract Top Matching Symbols & Graph Relationships
    if "symbols" in inc:
        for term in task.split()[:4]:
            if len(term) < 3:
                continue
            syms = graph_store.find_symbol(project_id, term)
            for s in syms[:4]:
                if not any(r["id"] == s["id"] for r in relevant_symbols):
                    relevant_symbols.append(s)
                    refs = graph_store.get_symbol_references(project_id, s["symbol_name"])
                    relationships.extend(refs[:3])

    # 3. Database Schema
    if "database_schema" in inc:
        schema = store.get_project_auth_config(project_id) or {}
        if schema:
            db_schema.append(schema)
            used_chars += len(json.dumps(schema))

    missing: list[str] = []
    if not relevant_files:
        missing.append("No directly matching source files found for query")
    if not db_schema and "database_schema" in inc:
        missing.append("No database schema discovered yet")

    return AssembledContext(
        project_id=project_id,
        task=task,
        project_summary=summary,
        relevant_files=relevant_files[:15],
        relevant_symbols=relevant_symbols[:15],
        relationships=relationships[:20],
        database_schema=db_schema,
        code_snippets=code_snippets,
        context_tokens=used_chars // 4,
        missing_information=missing,
    )
