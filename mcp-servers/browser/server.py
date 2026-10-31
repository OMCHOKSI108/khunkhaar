"""KHUNKHAAR Browser Automation MCP server.

Wraps the persistent Playwright session (core/browser/session.py) so an agent can drive a real
browser step by step and leave behind screenshots, traces, videos and network logs as evidence.

Run: ~/KHUNKHAAR-QA/.venv/bin/python ~/KHUNKHAAR-QA/mcp-servers/browser/server.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.browser.session import DEVICES, SESSION, SessionError  # noqa: E402
from core.shared.config import current  # noqa: E402
from core.shared.netguard import host_matches, normalize_host  # noqa: E402
from core.shared.redaction import redact_text  # noqa: E402
from core.shared.toollog import get_logger  # noqa: E402

logger = get_logger("mcp_browser", "mcp_browser.log")

server = MCPServer(
    name="khunkhaar-browser",
    version="0.1.0",
    instructions=(
        "Drives a real headless browser for QA. One session at a time: call browser_open, then "
        "browser_navigate/browser_act/browser_read, then browser_close to write the trace. "
        "Screenshots, traces, videos and downloads are saved under evidence/browser/<run_id>/."
    ),
)


def _strip_origin(entry: str) -> str:
    value = entry.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/")[0]


def _origin_allowed(url: str) -> tuple[bool, str]:
    cfg = current()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, f"scheme '{parts.scheme or 'none'}' is not navigable"
    allowed = cfg.get("browser", "allowed_origins", []) or []
    if not allowed:
        return True, "no origin restriction configured"
    host = normalize_host(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    for entry in allowed:
        if host_matches(_strip_origin(str(entry)), host, port):
            return True, f"matched {entry}"
    return False, f"host '{host}' is not in browser.allowed_origins"


def _err(message: str, kind: str = "error") -> dict[str, Any]:
    logger.info("BROWSER ERROR %s: %s", kind, redact_text(message)[:300])
    return {"ok": False, "error_kind": kind, "error": redact_text(message)[:600]}


@server.tool(description="Start a browser session. Supports chromium/firefox/webkit, headless mode, device presets for mobile emulation (desktop, iphone13, iphone14promax, pixel7, ipad, galaxy), video recording, tracing and a saved auth state to skip login.")
async def browser_open(url: str | None = None, engine: str | None = None, headless: bool = True,
                       device: str = "desktop", viewport: dict[str, int] | None = None,
                       record_video: bool = False, trace: bool = True,
                       storage_state: str | None = None) -> dict[str, Any]:
    cfg = current()
    if url:
        ok, reason = _origin_allowed(url)
        if not ok:
            return _err(f"navigation refused: {reason}", "origin_denied")
    try:
        info = await SESSION.start(
            engine=engine or cfg.get("browser", "default_engine", "chromium"),
            headless=cfg.get("browser", "headless", True) if headless is None else headless,
            device=device, viewport=viewport,
            record_video=record_video or cfg.get("browser", "video", False),
            trace=trace or cfg.get("browser", "tracing", True),
            storage_state=storage_state,
        )
    except SessionError as exc:
        return _err(str(exc), "launch_failed")
    out = {"ok": True, **info, "device_presets": sorted(DEVICES)}
    if url:
        out["navigation"] = await SESSION.goto(url, timeout_ms=cfg.get("browser", "navigation_timeout_ms", 30000))
    return out


@server.tool(description="Navigate the current session to a URL and report status, title and load time.")
async def browser_navigate(url: str, wait_until: str = "domcontentloaded",
                           timeout_ms: int | None = None) -> dict[str, Any]:
    ok, reason = _origin_allowed(url)
    if not ok:
        return _err(f"navigation refused: {reason}", "origin_denied")
    try:
        nav = await SESSION.goto(url, wait_until=wait_until,
                                 timeout_ms=timeout_ms or current().get("browser", "navigation_timeout_ms", 30000))
        return {"ok": True, **nav}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "navigation_failed")


@server.tool(description="Read the current page: url, title, visible text (truncated), element count.")
async def browser_read(max_chars: int = 20000) -> dict[str, Any]:
    try:
        return {"ok": True, **await SESSION.content_summary(max_chars=max_chars)}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "read_failed")


@server.tool(description="Return the computed accessibility tree (roles and accessible names) - the cheapest way to check that a page is navigable by assistive tech.")
async def browser_a11y_tree(max_nodes: int = 120) -> dict[str, Any]:
    try:
        return {"ok": True, "nodes": await SESSION.accessibility_tree(max_nodes)}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "a11y_failed")


@server.tool(description="Run an axe-core accessibility scan on the current page. Returns violations grouped by impact plus the raw report path under evidence/browser/<run_id>/a11y/. Optional include selector and WCAG tag filter (e.g. ['wcag2a','wcag2aa']).")
async def browser_a11y_scan(include: str | None = None, tags: list[str] | None = None,
                            max_nodes: int = 5) -> dict[str, Any]:
    try:
        return {"ok": True, **await SESSION.a11y_scan(include, tags, max_nodes)}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "a11y_scan_failed")


@server.tool(description="Capture a screenshot into evidence/browser/<run_id>/screenshots and return its relative path.")
async def browser_screenshot(name: str = "screenshot.png", full_page: bool = True,
                             selector: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, "path": await SESSION.screenshot(name, full_page, selector)}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "screenshot_failed")


@server.tool(description="Interact with the page: click, dblclick, fill, type, press, hover, focus, select, check, uncheck, scroll, wait_for_selector, reload, navigate_back, navigate_forward.")
async def browser_act(action: str, selector: str | None = None, value: str | None = None,
                      timeout_ms: int = 10000, screenshot_after: bool = False) -> dict[str, Any]:
    try:
        out = await SESSION.act(action, selector, value, timeout_ms)
        if screenshot_after:
            out["screenshot"] = await SESSION.screenshot(f"after-{action}.png")
        return {"ok": True, **out}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "action_failed")


@server.tool(description="Evaluate JavaScript in the page and return a truncated string result. Use for state assertions, not for mutation.")
async def browser_evaluate(expression: str, max_chars: int = 5000) -> dict[str, Any]:
    try:
        return {"ok": True, "result": await SESSION.evaluate(expression, max_chars)}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "evaluate_failed")


@server.tool(description="Return captured browser console messages and page errors for the current session.")
async def browser_console(limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "count": len(SESSION.console), "messages": await SESSION.console_summary(limit)}


@server.tool(description="Return captured network requests/responses (method, url, status, content-type) for the current session.")
async def browser_network(limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "count": len(SESSION.requests), "requests": await SESSION.network_summary(limit)}


@server.tool(description="Return files the browser downloaded during this session and where they were saved.")
async def browser_downloads() -> dict[str, Any]:
    return {"ok": True, "count": len(SESSION.downloads), "downloads": list(SESSION.downloads)}


@server.tool(description="Persist cookies and local storage as a named auth state so later sessions can start already logged in.")
async def browser_save_auth_state(name: str) -> dict[str, Any]:
    try:
        return {"ok": True, "storage_state": await SESSION.save_auth_state(name),
                "reuse_with": f"browser_open(storage_state='{name}.json')"}
    except SessionError as exc:
        return _err(str(exc), "session")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", "save_failed")


@server.tool(description="Close the session and flush the trace file (and video if recorded). Returns evidence paths.")
async def browser_close(save_trace: bool = True) -> dict[str, Any]:
    summary = await SESSION.stop(save_trace=save_trace)
    logger.info("session closed run_id=%s trace=%s", summary.get("run_id"), summary.get("trace"))
    return {"ok": True, **summary}


@server.tool(description="Report whether a session is open, its engine, run id and how long it has been alive.")
async def browser_status() -> dict[str, Any]:
    import time

    return {
        "ok": True,
        "active": SESSION.active,
        "engine": SESSION.engine or (SESSION.browser and "launched"),
        "run_id": SESSION.run_id,
        "url": SESSION.page.url if SESSION.page else None,
        "tracing_on": SESSION.tracing_on,
        "console_messages": len(SESSION.console),
        "network_events": len(SESSION.requests),
        "uptime_seconds": round(time.monotonic() - SESSION.started_at, 1) if SESSION.started_at else 0,
    }


if __name__ == "__main__":
    logger.info("starting khunkhaar-browser MCP server over stdio")
    server.run(transport="stdio")
