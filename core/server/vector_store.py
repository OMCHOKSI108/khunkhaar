"""Vector Store and Codebase RAG Engine for Khunkhaar QA."""
from __future__ import annotations

import json
import math
import os
import urllib.request
from pathlib import Path
from typing import Any

from core.server.store import _get_conn

EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
CF_AI_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"


def init_vector_table() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS code_vector_embeddings (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            chunk_type TEXT,
            content TEXT,
            embedding TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_vec_proj ON code_vector_embeddings(project_id);
        """)



def _fallback_hash_embedding(text: str, dim: int = 128) -> list[float]:
    """Deterministic normalized bag-of-hashes embedding for offline fallback."""
    vec = [0.0] * dim
    for word in text.lower().split():
        h = hash(word) % dim
        vec[h] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Compute dense embeddings using Cloudflare Workers AI with offline fallback."""
    token = os.environ.get("CF_API_TOKEN", "")
    account = os.environ.get("CF_ACCOUNT_ID", "")
    if token and account:
        try:
            url = CF_AI_URL.format(account=account, model=EMBEDDING_MODEL)
            req = urllib.request.Request(
                url, data=json.dumps({"text": texts[:16]}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success") and "result" in data:
                    res = data["result"].get("data") or data["result"]
                    if isinstance(res, list) and res and isinstance(res[0], list):
                        return res
        except Exception:
            pass
    return [_fallback_hash_embedding(t) for t in texts]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)


def index_codebase_chunks(project_id: str, codebase_path: Path, max_files: int = 50) -> int:
    """Chunk and index source files into SQLite Vector DB."""
    init_vector_table()
    skip = {".git", "node_modules", "dist", "build", "coverage", ".venv", "__pycache__"}
    exts = {".js", ".ts", ".py", ".graphql", ".sql", ".prisma"}
    chunks, count = [], 0
    for p in codebase_path.rglob("*"):
        if count >= max_files or any(d in p.parts for d in skip) or not p.is_file() or p.suffix not in exts:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(p.relative_to(codebase_path))
        for i in range(0, len(lines), 30):
            block = "\n".join(lines[i:i + 40]).strip()
            if len(block) > 40:
                chunks.append({
                    "id": f"{project_id}-{hash(rel + str(i))}", "project_id": project_id,
                    "file_path": rel, "line_start": i + 1, "line_end": min(i + 40, len(lines)),
                    "chunk_type": p.suffix.lstrip("."), "content": block[:1500]
                })
        count += 1
    if not chunks:
        return 0
    texts = [c["content"] for c in chunks]
    embeddings = [emb for i in range(0, len(texts), 16) for emb in embed_texts(texts[i:i + 16])]
    with _get_conn() as conn:
        conn.execute("DELETE FROM code_vector_embeddings WHERE project_id = ?", (project_id,))
        conn.executemany(
            """INSERT INTO code_vector_embeddings 
               (id, project_id, file_path, line_start, line_end, chunk_type, content, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(c["id"], c["project_id"], c["file_path"], c["line_start"], c["line_end"],
              c["chunk_type"], c["content"], json.dumps(emb)) for c, emb in zip(chunks, embeddings)]
        )
    return len(chunks)


def search_code_context(project_id: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Retrieve top-K most semantically relevant code chunks via vector similarity."""
    init_vector_table()
    q_emb = embed_texts([query])[0]
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT file_path, line_start, line_end, chunk_type, content, embedding "
            "FROM code_vector_embeddings WHERE project_id = ?", (project_id,)
        ).fetchall()
    scored = []
    for r in rows:
        try:
            score = _cosine_similarity(q_emb, json.loads(r["embedding"]))
            scored.append({
                "file_path": r["file_path"], "lines": f"{r['line_start']}-{r['line_end']}",
                "chunk_type": r["chunk_type"], "content": r["content"], "score": round(score, 4)
            })
        except Exception:
            continue
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]

