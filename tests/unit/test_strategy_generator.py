"""Tests for adaptive 18-category test strategy generation."""

from pathlib import Path
import pytest

from core.recon.models import ProjectRecon, EndpointDef, ModelDef, PortInfo
from core.strategy.generator import generate_test_strategy


def test_generate_strategy_with_sqli_and_idor():
    endpoints = [
        EndpointDef(
            name="searchUsers",
            kind="graphql_query",
            path="/graphql#searchUsers",
            method="POST",
            auth_required=True,
            description="Query returning User. Args: search: String, filter: UserFilter",
        ),
        EndpointDef(
            name="getUserDetails",
            kind="graphql_query",
            path="/graphql#getUserDetails",
            method="POST",
            auth_required=True,
            description="Query returning User. Args: id: ID!",
        ),
        EndpointDef(
            name="approveLeaveRequest",
            kind="graphql_mutation",
            path="/graphql#approveLeaveRequest",
            method="POST",
            auth_required=True,
            description="Mutation returning Boolean. Args: id: ID!",
        ),
    ]

    recon = ProjectRecon(
        project_id="test1234",
        project_name="MockHRApp",
        root_path="/mock/path",
        primary_language="TypeScript",
        project_type="graphql-api",
        endpoints=endpoints,
    )

    strategy = generate_test_strategy(recon)
    assert strategy.total_count >= 3
    assert strategy.p0_count > 0

    categories = {tc.category for tc in strategy.test_cases}
    assert any("SQL Injection" in cat for cat in categories)
    assert any("IDOR" in cat for cat in categories)
    assert any("RBAC" in cat for cat in categories)
