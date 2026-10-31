"""Browser toolchain acceptance tests (Phase 1 section 12, "Browser").

A real page loads, a screenshot is generated and a browser trace is generated - and the
navigation guard refuses non-http(s) schemes and origins outside browser.allowed_origins.

Everything is driven through the MCP tool functions, i.e. the surface an agent actually calls.
The session singleton must be closed inside the same event loop that opened it, so every flow
runs through `session()`, which closes in a finally block.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from core.shared import paths

pytestmark = pytest.mark.browser


@asynccontextmanager
async def session(browser_mcp, mock_server: str | None = None, **open_kwargs):
    opened = await browser_mcp.browser_open(url=mock_server, **open_kwargs)
    try:
        yield opened
    finally:
        if browser_mcp.SESSION.active or opened.get("ok"):
            await browser_mcp.browser_close()


async def _close(browser_mcp):
    closed = await browser_mcp.browser_close()
    assert closed["ok"], closed
    return closed


# ---------------------------------------------------------------- load & evidence


async def test_real_page_loads_and_reads(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        assert opened["navigation"]["status"] == 200
        assert opened["navigation"]["title"] == "KHUNKHAAR Mock App"

        read = await browser_mcp.browser_read()
        assert read["ok"] and read["element_count"] > 10
        assert "Sign in" in read["text"]


async def test_screenshot_and_trace_are_written_to_evidence(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        run_id = opened["run_id"]

        shot = await browser_mcp.browser_screenshot(name="acceptance.png")
        assert shot["ok"], shot
        shot_path = paths.kh_root() / shot["path"]
        assert shot_path.is_file(), shot_path
        assert shot_path.stat().st_size > 5_000, "screenshot is suspiciously small"

        closed = await _close(browser_mcp)
        assert closed["trace"], "trace was not recorded"
        trace_path = paths.kh_root() / closed["trace"]
        assert trace_path.is_file(), trace_path
        assert trace_path.stat().st_size > 5_000, "trace.zip is suspiciously small"
        assert run_id in closed["trace"]
        for rel in (shot["path"], closed["trace"]):
            assert rel.startswith("evidence/")
            assert str(paths.kh_root()) not in rel, "absolute home path leaked into evidence"


async def test_video_recording_produces_a_file(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server, record_video=True) as opened:
        assert opened["ok"], opened
        await browser_mcp.browser_navigate(url=f"{mock_server}/items")
        closed = await _close(browser_mcp)
    videos = [v for v in closed["videos"] if isinstance(v, str) and not v.startswith("<")]
    assert videos, closed["videos"]
    for video in videos:
        file = paths.kh_root() / video
        assert file.is_file() and file.stat().st_size > 1_000, video


# ---------------------------------------------------------------- interaction


async def test_form_flow_fills_submits_and_reads_the_result(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        for action, selector, value in (
            ("fill", "#email", "qa@example.com"),
            ("fill", "#password", "correct-horse-battery"),
            ("select", "#role", "admin"),
        ):
            step = await browser_mcp.browser_act(action=action, selector=selector, value=value)
            assert step["ok"], step

        clicked = await browser_mcp.browser_act(action="click", selector="#submit")
        assert clicked["ok"], clicked

        result = await browser_mcp.browser_evaluate(
            expression="document.getElementById('result').textContent")
        assert result["ok"] and result["result"].strip() == "signed in as qa@example.com", result

        role = await browser_mcp.browser_evaluate(expression="document.getElementById('role').value")
        assert role["result"] == "admin", role


async def test_console_and_network_are_captured(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        await browser_mcp.browser_act(action="fill", selector="#email", value="net@example.com")
        await browser_mcp.browser_act(action="fill", selector="#password", value="pw")
        await browser_mcp.browser_act(action="click", selector="#submit")

        console = await browser_mcp.browser_console()
        assert console["ok"] and console["count"] > 0
        assert any("login submitted" in str(m.get("text")) for m in console["messages"]), console["messages"]

        network = await browser_mcp.browser_network()
        assert network["ok"] and network["count"] > 0
        urls = [str(entry.get("url")) for entry in network["requests"]]
        assert any(url.endswith("/health") for url in urls), urls
        statuses = {entry.get("status") for entry in network["requests"] if entry.get("status")}
        assert 200 in statuses, network["requests"]


async def test_accessibility_tree_exposes_landmarks_and_labels(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        tree = await browser_mcp.browser_a11y_tree()
        assert tree["ok"], tree
        flat = str(tree["nodes"])
        assert "Sign in" in flat
        assert "Email" in flat


async def test_auth_state_can_be_saved_and_reused(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        await browser_mcp.browser_act(action="fill", selector="#email", value="auth@example.com")
        await browser_mcp.browser_act(action="fill", selector="#password", value="pw")
        await browser_mcp.browser_act(action="click", selector="#submit")
        saved = await browser_mcp.browser_save_auth_state(name="phase1-smoke")
        assert saved["ok"], saved
        state_path = paths.kh_root() / saved["storage_state"]
        assert state_path.is_file()
        assert state_path.stat().st_mode & 0o077 == 0, "auth state must not be world-readable"
        content = state_path.read_text()
        assert "correct-horse-battery" not in content


# ---------------------------------------------------------------- engines & emulation


@pytest.mark.parametrize("engine", ["chromium", "firefox"])
async def test_engine_launches_and_loads_a_page(browser_mcp, mock_server, engine):
    async with session(browser_mcp, mock_server, engine=engine) as opened:
        assert opened["ok"], opened
        assert opened["engine"] == engine
        assert opened["navigation"]["status"] == 200
        assert opened["browser_version"]


async def test_mobile_viewport_emulation(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server, device="iphone13") as opened:
        assert opened["ok"], opened
        assert opened["viewport"] == {"width": 390, "height": 664}, opened["viewport"]

        ua = await browser_mcp.browser_evaluate(expression="navigator.userAgent")
        assert "iPhone" in ua["result"], ua

        metrics = await browser_mcp.browser_evaluate(
            expression="[window.devicePixelRatio, navigator.maxTouchPoints, 'ontouchstart' in window].join('/')")
        assert metrics["result"].startswith("3/"), metrics

        # window.innerWidth is the *layout* viewport, which Chromium inflates to 980 because the
        # mock app has no <meta name="viewport"> tag. The emulated (device) viewport above is the
        # contract; a responsive page must be checked with a viewport meta tag present.
        layout = await browser_mcp.browser_evaluate(expression="window.innerWidth + 'x' + window.innerHeight")
        assert layout["result"].startswith("980x"), layout


async def test_custom_viewport_overrides_desktop_preset(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server, viewport={"width": 1024, "height": 768}) as opened:
        assert opened["ok"], opened
        assert opened["viewport"] == {"width": 1024, "height": 768}, opened["viewport"]


async def test_a11y_scan_runs_axe_and_writes_evidence(browser_mcp, mock_server):
    async with session(browser_mcp, mock_server) as opened:
        assert opened["ok"], opened
        scan = await browser_mcp.browser_a11y_scan()
        assert scan["ok"], scan
        assert set(scan["by_impact"]) == {"critical", "serious", "moderate", "minor"}
        assert scan["violation_count"] == len(scan["violations"])
        report = paths.kh_root() / scan["evidence"]
        assert report.is_file(), scan["evidence"]
        assert report.stat().st_size > 0
        for violation in scan["violations"]:
            assert violation["id"] and violation["impact"] in scan["by_impact"]

        filtered = await browser_mcp.browser_a11y_scan(tags=["wcag2aa"])
        assert filtered["ok"], filtered
        assert all("wcag2aa" in v["tags"] for v in filtered["violations"])


async def test_webkit_launches_or_reports_a_structured_failure(browser_mcp, mock_server):
    """WebKit works here after scripts/install_webkit_deps.sh; on a host without the libs,
    the tool must surface a clean launch_failed result instead of a traceback or a hang.
    """
    result = await browser_mcp.browser_open(url=mock_server, engine="webkit")
    if result["ok"]:
        assert result["navigation"]["status"] == 200
        await browser_mcp.browser_close()
        assert not browser_mcp.SESSION.active
    else:
        assert result["error_kind"] == "launch_failed"
        assert "Traceback" not in str(result)
        assert not browser_mcp.SESSION.active

    async with session(browser_mcp, mock_server, engine="chromium") as opened:
        assert opened["ok"], opened
        assert opened["navigation"]["status"] == 200


# ---------------------------------------------------------------- navigation guard


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:text/html,<h1>hi</h1>",
    "chrome://settings",
    "javascript:alert(1)",
    "/relative/path",
])
async def test_non_http_schemes_are_refused(browser_mcp, url):
    """The scheme check must run before the empty-allowlist shortcut, otherwise file:// slips through."""
    opened = await browser_mcp.browser_open(url=url)
    assert opened["ok"] is False and opened["error_kind"] == "origin_denied", opened
    assert not browser_mcp.SESSION.active, "a refused open must not launch a browser"

    navigated = await browser_mcp.browser_navigate(url=url)
    assert navigated["ok"] is False and navigated["error_kind"] == "origin_denied", navigated


async def test_origins_outside_the_allowlist_are_refused(browser_mcp):
    from core.shared.config import current

    assert current().get("browser", "allowed_origins"), "allowlist must be configured for this test"
    for url in ("http://example.com/", "https://www.google.com/", "http://127.0.0.1:9999/"):
        result = await browser_mcp.browser_navigate(url=url)
        assert result["ok"] is False and result["error_kind"] == "origin_denied", (url, result)
    assert not browser_mcp.SESSION.active
    assert Path("/etc/passwd").is_file()  # the guard, not the OS, is what stopped file://
