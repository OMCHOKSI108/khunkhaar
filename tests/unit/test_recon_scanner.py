"""Tests for universal reconnaissance and scanner."""

from pathlib import Path
import tempfile
import pytest

from core.recon.scanner import (
    detect_frameworks_and_language,
    extract_graphql_endpoints,
    extract_database_models,
    scan_project,
)


def test_detect_python_fastapi_framework(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi==0.100.0\nuvicorn\nsqlalchemy\n", encoding="utf-8")
    lang, frameworks, proj_type = detect_frameworks_and_language(tmp_path)
    from core.recon.scanner import detect_python_and_other_frameworks
    lang, frameworks, proj_type = detect_python_and_other_frameworks(tmp_path, lang, frameworks, proj_type)

    assert lang == "Python"
    assert "FastAPI" in frameworks
    assert "SQLAlchemy" in frameworks
    assert proj_type == "rest-api"


def test_extract_graphql_endpoints_from_schema(tmp_path: Path):
    schema = tmp_path / "schema.graphql"
    schema.write_text(
        """
        type Query {
            getUser(id: ID!): User @isAuthenticated
            searchProjects(term: String): [Project]
        }
        type Mutation {
            approveRequest(id: ID!): Boolean @hasRole(role: "ADMIN")
        }
        """,
        encoding="utf-8",
    )
    endpoints = extract_graphql_endpoints(tmp_path)
    assert len(endpoints) == 3
    names = {e.name for e in endpoints}
    assert "getUser" in names
    assert "searchProjects" in names
    assert "approveRequest" in names
