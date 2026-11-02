"""Benchmark and integration tests for Accessibility & UX Intelligence Engine v2."""
from __future__ import annotations

from contextlib import asynccontextmanager
import pytest
from core.shared import paths

pytestmark = pytest.mark.browser


@asynccontextmanager
async def browser_session(browser_mcp, mock_server: str):
    opened = await browser_mcp.browser_open(url=mock_server)
    try:
        yield opened
    finally:
        if browser_mcp.SESSION.active:
            await browser_mcp.browser_close()


async def test_keyboard_navigation_inspect(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        res = await browser_mcp.browser_keyboard_inspect(max_tabs=15)
        assert res["ok"] is True
        assert res["count"] > 0
        first = res["sequence"][0]
        assert "tag" in first
        assert "visible" in first


async def test_focus_graph_inspection(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        res = await browser_mcp.browser_focus_inspect(max_tabs=15)
        assert res["ok"] is True
        assert res["node_count"] > 0
        assert "nodes" in res
        assert "edges" in res


async def test_form_accessibility_audit(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        res = await browser_mcp.browser_form_inspect()
        assert res["ok"] is True
        assert res["field_count"] >= 2
        # Forms in mock app should have fields parsed
        fields = res["fields"]
        assert any(f["type"] in ("text", "email") for f in fields)


async def test_responsive_layout_inspection(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        res = await browser_mcp.browser_responsive_inspect(viewport="small_mobile")
        assert res["ok"] is True
        assert res["viewport"]["width"] == 320
        assert "horizontal_overflow" in res
        assert "defects" in res


async def test_user_journey_execution_success(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        steps = [
            {"action": "navigate", "target": mock_server, "assertion": "text_present:Sign in"},
            {"action": "fill", "target": "#email", "value": "test@example.com"},
            {"action": "fill", "target": "#password", "value": "secret"},
        ]
        res = await browser_mcp.browser_journey_execute("login_flow", steps)
        assert res["ok"] is True
        assert res["success"] is True
        assert res["steps_completed"] == 3


async def test_user_journey_assertion_failure(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        steps = [
            {"action": "navigate", "target": mock_server, "assertion": "text_present:NON_EXISTENT_TEXT_XYZ_123"},
        ]
        res = await browser_mcp.browser_journey_execute("failing_journey", steps)
        assert res["ok"] is True
        assert res["success"] is False
        assert res["failed_step_index"] == 1
        assert "assertion failed" in res["error_message"]


async def test_full_ux_report_generation(browser_mcp, mock_server):
    async with browser_session(browser_mcp, mock_server):
        res = await browser_mcp.browser_ux_report(profile="wcag_22_aa")
        assert res["ok"] is True
        rep = res["report"]
        assert rep["url"].rstrip("/") == mock_server.rstrip("/")
        assert rep["keyboard_steps"] > 0
        assert rep["forms_inspected"] >= 2
        assert "total_issues_found" in rep
        assert isinstance(rep["issues"], list)
