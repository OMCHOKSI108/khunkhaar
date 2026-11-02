"""Vector Store and Codebase RAG Engine for Khunkhaar QA with unique context ID mapping."""
from __future__ import annotations

import json
import math
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from core.server.store import _get_conn

EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
CF_AI_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"


def init_vector_table() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS project_contexts (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, context_name TEXT NOT NULL,
            total_symbols INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0,
            frameworks TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_pctx_proj ON project_contexts(project_id);

        CREATE TABLE IF NOT EXISTS code_vector_embeddings (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, context_id TEXT,
            file_path TEXT NOT NULL, symbol_name TEXT, symbol_type TEXT,
            line_start INTEGER, line_end INTEGER, content TEXT,
            content_hash TEXT, metadata TEXT, embedding TEXT
        );
        """)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(code_vector_embeddings)").fetchall()}
        for col in ("context_id", "symbol_name", "symbol_type", "content_hash", "metadata"):
            if col not in cols:
                conn.execute(f"ALTER TABLE code_vector_embeddings ADD COLUMN {col} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_ctx ON code_vector_embeddings(context_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_proj ON code_vector_embeddings(project_id)")


def create_project_context(project_id: str, name: str, symbols: int, frameworks: list[str]) -> str:
    init_vector_table()
    ctx_id = f"ctx-{uuid.uuid4().hex[:12]}"
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO project_contexts (id, project_id, context_name, total_symbols, frameworks) VALUES (?,?,?,?,?)",
            (ctx_id, project_id, name, symbols, json.dumps(frameworks)),
        )
    return ctx_id


def get_project_contexts(project_id: str) -> list[dict[str, Any]]:
    init_vector_table()
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM project_contexts WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
        return [dict(r) for r in rows]


def _fallback_hash_embedding(text: str, dim: int = 128) -> list[float]:
    vec = [0.0] * dim
    for w in text.lower().split(): vec[hash(w) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_texts(texts: list[str]) -> list[list[float]]:
    token, account = os.environ.get("CF_API_TOKEN", ""), os.environ.get("CF_ACCOUNT_ID", "")
    if token and account:
        try:
            url = CF_AI_URL.format(account=account, model=EMBEDDING_MODEL)
            req = urllib.request.Request(
                url, data=json.dumps({"text": texts[:16]}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success") and "result" in data:
                    res = data["result"].get("data") or data["result"]
                    if isinstance(res, list) and res and isinstance(res[0], list): return res
        except Exception: pass
    return [_fallback_hash_embedding(t) for t in texts]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b): return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return (dot / norm) if norm > 0 else 0.0


def index_semantic_chunks(project_id: str, context_id: str, chunks: list[dict[str, Any]]) -> int:
    init_vector_table()
    if not chunks: return 0
    texts = [c["content"] for c in chunks]
    embeddings = [emb for i in range(0, len(texts), 16) for emb in embed_texts(texts[i:i + 16])]
    with _get_conn() as conn:
        conn.execute("DELETE FROM code_vector_embeddings WHERE context_id = ?", (context_id,))
        conn.executemany(
            """INSERT INTO code_vector_embeddings
               (id, project_id, context_id, file_path, symbol_name, symbol_type,
                line_start, line_end, content, content_hash, metadata, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(f"{context_id}-{i}", project_id, context_id, c["file_path"], c.get("symbol_name"),
              c.get("symbol_type"), c["line_start"], c["line_end"], c["content"],
              c.get("content_hash", ""), json.dumps(c.get("metadata", {})), json.dumps(emb))
             for i, (c, emb) in enumerate(zip(chunks, embeddings))]
        )
        conn.execute("UPDATE project_contexts SET total_chunks = ? WHERE id = ?", (len(chunks), context_id))
    return len(chunks)


def index_codebase_chunks(project_id: str, codebase_path: Path, max_files: int = 50) -> int:
    ctx_id = create_project_context(project_id, "default", 0, [])
    from core.context_builder import scanner, semantic_chunker
    files = scanner.scan_directory(codebase_path, max_files=max_files)
    chunks: list[dict[str, Any]] = []
    for f in files:
        chunks.extend(semantic_chunker.chunk_file_by_ast("default", Path(f.path), f.relative_path, []))
    return index_semantic_chunks(project_id, ctx_id, chunks)


def search_code_context(project_id: str, query: str, top_k: int = 5, context_id: str | None = None) -> list[dict[str, Any]]:
    init_vector_table()
    q_emb = embed_texts([query])[0]
    sql = "SELECT * FROM code_vector_embeddings WHERE project_id = ?"
    params: list[Any] = [project_id]
    if context_id:
        sql += " AND context_id = ?"
        params.append(context_id)
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    scored = []
    for r in rows:
        try:
            score = _cosine_similarity(q_emb, json.loads(r["embedding"]))
            scored.append({
                "context_id": r["context_id"], "file_path": r["file_path"],
                "symbol_name": r["symbol_name"], "symbol_type": r["symbol_type"],
                "lines": f"{r['line_start']}-{r['line_end']}", "content": r["content"], "score": round(score, 4),
            })
        except Exception: continue
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
