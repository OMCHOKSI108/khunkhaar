"""Hybrid search engine combining vector embeddings, keyword BM25/FTS, and symbol matching."""
from __future__ import annotations

import sqlite3
from typing import Any

from core.context_builder import graph_store
from core.context_builder.models import SearchResult
from core.server import vector_store
from core.shared import paths


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.metadata_db(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _search_keywords(project_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    tokens = [t.strip().lower() for t in query.split() if len(t.strip()) > 2]
    if not tokens:
        return []
    like_clause = " OR ".join(["content LIKE ?"] * len(tokens))
    params = [f"%{t}%" for t in tokens]
    with _get_conn() as conn:
        try:
            rows = conn.execute(
                f"SELECT file_path, chunk_index, content FROM code_vector_embeddings "
                f"WHERE project_id = ? AND ({like_clause}) LIMIT {limit}",
                [project_id] + params,
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def hybrid_search(project_id: str, query: str, top_k: int = 10) -> list[SearchResult]:
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    # 1. Vector Search
    try:
        vec_results = vector_store.search_code_context(project_id, query, top_k=top_k * 2)
        for rank, res in enumerate(vec_results):
            key = f"{res['file_path']}:{res.get('chunk_index', 0)}"
            scores[key] = scores.get(key, 0.0) + (1.0 / (60 + rank))
            items[key] = {
                "file_path": res["file_path"],
                "start_line": res.get("chunk_index", 0) * 30 + 1,
                "end_line": res.get("chunk_index", 0) * 30 + 35,
                "content": res.get("content", ""),
                "match_type": "vector",
            }
    except Exception:
        pass

    # 2. Keyword Search
    kw_results = _search_keywords(project_id, query, limit=top_k * 2)
    for rank, res in enumerate(kw_results):
        key = f"{res['file_path']}:{res.get('chunk_index', 0)}"
        scores[key] = scores.get(key, 0.0) + (1.0 / (60 + rank))
        if key not in items:
            items[key] = {
                "file_path": res["file_path"],
                "start_line": res.get("chunk_index", 0) * 30 + 1,
                "end_line": res.get("chunk_index", 0) * 30 + 35,
                "content": res.get("content", ""),
                "match_type": "keyword",
            }

    # 3. Symbol Match
    sym_results = graph_store.find_symbol(project_id, query)
    for rank, sym in enumerate(sym_results[:5]):
        key = f"{sym['file_path']}:{sym['start_line']}"
        scores[key] = scores.get(key, 0.0) + (1.5 / (60 + rank))
        if key not in items:
            items[key] = {
                "file_path": sym["file_path"],
                "start_line": sym["start_line"],
                "end_line": sym["end_line"],
                "content": f"{sym['signature']}\n{sym.get('docstring') or ''}".strip(),
                "match_type": "symbol",
                "symbol_name": sym["symbol_name"],
            }

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)[:top_k]
    results: list[SearchResult] = []
    for k in sorted_keys:
        info = items[k]
        results.append(
            SearchResult(
                file_path=info["file_path"],
                start_line=info["start_line"],
                end_line=info["end_line"],
                content=info["content"],
                score=scores[k],
                match_type=info["match_type"],
                symbol_name=info.get("symbol_name", ""),
            )
        )
    return results
