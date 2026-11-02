"""Unit tests for Codebase Vector Store and RAG retrieval."""
from __future__ import annotations

import tempfile
from pathlib import Path

from core.server import vector_store


def test_fallback_hash_embedding():
    emb = vector_store._fallback_hash_embedding("Hello world auth user")
    assert len(emb) == 128
    assert abs(sum(x * x for x in emb) - 1.0) < 1e-4


def test_cosine_similarity():
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    assert abs(vector_store._cosine_similarity(v1, v2) - 1.0) < 1e-4
    assert abs(vector_store._cosine_similarity(v1, v3) - 0.0) < 1e-4


def test_index_and_search_codebase():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        test_file = tmp_path / "auth_service.js"
        test_file.write_text(
            "// Auth Service\n"
            "function authenticateUser(email, password) {\n"
            "    const token = generateJWT(email);\n"
            "    return { status: 'success', token };\n"
            "}\n" * 5
        )
        project_id = "test_rag_proj"
        indexed = vector_store.index_codebase_chunks(project_id, tmp_path)
        assert indexed >= 1

        results = vector_store.search_code_context(project_id, "authenticate user token", top_k=2)
        assert len(results) >= 1
        assert "auth_service.js" in results[0]["file_path"]
