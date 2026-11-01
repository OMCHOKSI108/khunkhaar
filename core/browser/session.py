"""Persistent Playwright session used by the browser MCP server.

One session survives across MCP tool calls so an agent can open a page, click, and capture
evidence step by step. Uses the async API because MCP tools execute inside the server's event
loop; the sync API would bind the driver to whichever worker thread ran first.

Capabilities required by spec 4.A: headless execution, screenshots, video recording, trace
recording, network inspection, console inspection, download handling, storage-state (auth)
handling, and mobile viewport emulation.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Error as PlaywrightError

from core.shared import evidence as ev
from core.shared import paths
from core.shared.redaction import redact_text


def ensure_browser_cache_path() -> None:
    """Ensure Playwright discovers installed browser binaries if XDG_CACHE_HOME is relocated."""
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    home_cache = Path.home() / ".cache" / "ms-playwright"
    xdg_env = os.environ.get("XDG_CACHE_HOME")
    if xdg_env and home_cache.is_dir():
        xdg_cache = Path(xdg_env).expanduser() / "ms-playwright"
        try:
            home_count = len(list(home_cache.iterdir()))
            xdg_count = len(list(xdg_cache.iterdir())) if xdg_cache.is_dir() else 0
            if home_count > xdg_count:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(home_cache)
        except OSError:
            pass

# Values read from the installed playwright-core device descriptors
# (`node -e "console.log(require('playwright-core').devices)"`). The Python package has no
# `playwright.devices` accessor, so the table is vendored rather than looked up at runtime.
DEVICES: dict[str, dict[str, Any] | None] = {
    "desktop": None,
    "iphone13": {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1",
        "viewport": {"width": 390, "height": 664}, "device_scale_factor": 3,
        "is_mobile": True, "has_touch": True},
    "iphone14promax": {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1",
        "viewport": {"width": 430, "height": 740}, "device_scale_factor": 3,
        "is_mobile": True, "has_touch": True},
    "pixel7": {
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.8078.4 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 839}, "device_scale_factor": 2.625,
        "is_mobile": True, "has_touch": True},
    "ipad": {
        "user_agent": "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1",
        "viewport": {"width": 834, "height": 1194}, "device_scale_factor": 2,
        "is_mobile": True, "has_touch": True},
    "galaxy": {
        "user_agent": "Mozilla/5.0 (Linux; Android 8.0.0; SM-G965U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.8078.4 Mobile Safari/537.36",
        "viewport": {"width": 320, "height": 658}, "device_scale_factor": 4.5,
        "is_mobile": True, "has_touch": True},
}


class SessionError(RuntimeError):
    pass


class BrowserSession:
    def __init__(self) -> None:
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.engine: str | None = None
        self.run_id: str | None = None
        self.console: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []
        self.tracing_on = False
        self.video_dir = None
        self.started_at: float | None = None

    @property
    def active(self) -> bool:
        return self.page is not None and self.context is not None

    def _require(self) -> None:
        if not self.active:
            raise SessionError("no browser session is open - call browser_open first")

    async def start(self, engine: str = "chromium", headless: bool = True,
                    device: str = "desktop", viewport: dict | None = None,
                    record_video: bool = False, trace: bool = True,
                    storage_state: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        if self.active:
            await self.stop()
        engine = (engine or "chromium").lower()
        if engine not in ("chromium", "firefox", "webkit"):
            raise SessionError(f"unknown engine '{engine}' (chromium, firefox, webkit)")

        self.run_id = run_id or ev.new_run_id("browser")
        ensure_browser_cache_path()
        self._pw = await async_playwright().start()
        launcher = getattr(self._pw, engine)
        try:
            self.browser = await launcher.launch(headless=headless)
        except PlaywrightError as exc:
            await self._pw.stop()
            self._pw = None
            raise SessionError(f"{engine} failed to launch: {str(exc).splitlines()[0][:300]}") from exc

        ctx_kwargs: dict[str, Any] = {}
        preset = DEVICES.get((device or "desktop").lower())
        if preset:
            ctx_kwargs.update(preset)
        elif viewport:
            ctx_kwargs["viewport"] = {"width": int(viewport.get("width", 1280)),
                                      "height": int(viewport.get("height", 720))}
        if storage_state:
            target = paths.evidence_dir("browser", "auth") / storage_state
            if not target.exists():
                raise SessionError(f"storage state '{storage_state}' not found under evidence/browser/auth/")
            ctx_kwargs["storage_state"] = str(target)
        if record_video:
            self.video_dir = paths.evidence_dir("browser", self.run_id, "video")
            self.video_dir.mkdir(parents=True, exist_ok=True)
            ctx_kwargs["record_video_dir"] = str(self.video_dir)

        self.engine = engine
        self.context = await self.browser.new_context(**ctx_kwargs)
        self.context.set_default_timeout(15_000)

        self.page = await self.context.new_page()
        self.page.on("console", lambda msg: self.console.append(
            {"type": msg.type, "text": msg.text[:500], "at": time.time()}))
        self.page.on("pageerror", lambda err: self.console.append(
            {"type": "pageerror", "text": str(err)[:500], "at": time.time()}))
        self.page.on("request", lambda r: self.requests.append(
            {"method": r.method, "url": r.url[:300], "resource_type": r.resource_type}))
        self.page.on("response", lambda r: self._record_response(r))
        self.page.on("download", lambda d: self._handle_download(d))

        if trace:
            await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self.tracing_on = True

        self.started_at = time.monotonic()
        return {
            "engine": engine,
            "browser_version": self.browser.version,
            "headless": headless,
            "device": device,
            "viewport": self.page.viewport_size,
            "tracing": self.tracing_on,
            "video": bool(record_video),
            "run_id": self.run_id,
        }

    def _record_response(self, response) -> None:
        self.requests.append({
            "url": response.url[:300], "status": response.status,
            "method": response.request.method,
            "content_type": (response.headers.get("content-type") or "")[:80],
            "duration_hint": None,
        })

    async def _handle_download(self, download) -> None:
        try:
            folder = paths.evidence_dir("browser", self.run_id or "downloads", "downloads")
            target = folder / download.suggested_filename
            await download.save_as(str(target))
            self.downloads.append({"filename": download.suggested_filename, "path": ev.relative(target)})
        except Exception as exc:
            self.downloads.append({"filename": download.suggested_filename, "error": type(exc).__name__})

    async def stop(self, save_trace: bool = True) -> dict[str, Any]:
        """Flush trace and video, then tear the session down.

        Order matters: a video file is only finalised once its page is closed, so the context is
        closed first and the paths are copied afterwards. Calling save_as() before close()
        deadlocks waiting on a page that will never finish.
        """
        import shutil

        summary: dict[str, Any] = {"run_id": self.run_id, "trace": None, "videos": [], "close_ms": None}
        if not self._pw:
            return summary
        started = time.monotonic()
        pending_videos: list[tuple[Any, Path]] = []
        try:
            if self.tracing_on and self.context and save_trace:
                trace_path = paths.evidence_dir("browser", self.run_id or "adhoc") / "trace.zip"
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                await self.context.tracing.stop(path=str(trace_path))
                summary["trace"] = ev.relative(trace_path)
                self.tracing_on = False
            if self.context and self.video_dir:
                for index, page in enumerate(self.context.pages):
                    if page.video:
                        pending_videos.append((page.video, self.video_dir / f"page-{index + 1}.webm"))
            if self.context:
                await self.context.close()
            for video, dest in pending_videos:
                try:
                    source = await video.path()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, dest)
                    summary["videos"].append(ev.relative(dest))
                except Exception as exc:
                    summary["videos"].append(f"<unavailable: {type(exc).__name__}>")
            if self.browser:
                await self.browser.close()
        finally:
            await self._pw.stop()
            summary["close_ms"] = round((time.monotonic() - started) * 1000, 2)
            self._pw = self.browser = self.context = self.page = None
            self.tracing_on = False
            self.video_dir = None
        return summary

    # ---- navigation & inspection -------------------------------------------
    async def goto(self, url: str, wait_until: str = "domcontentloaded",
                   timeout_ms: int = 30_000) -> dict[str, Any]:
        self._require()
        started = time.monotonic()
        response = await self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        return {
            "url": self.page.url,
            "status": response.status if response else None,
            "title": await self.page.title(),
            "load_ms": round((time.monotonic() - started) * 1000, 2),
        }

    async def screenshot(self, name: str = "screenshot.png", full_page: bool = True,
                         selector: str | None = None) -> str:
        self._require()
        folder = paths.evidence_dir("browser", self.run_id or "adhoc", "screenshots")
        target = folder / ev.slugify(name, 80).replace("-", "_")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.suffix:
            target = target.with_suffix(".png")
        if selector:
            await (await self.page.wait_for_selector(selector, timeout=10_000)).screenshot(path=str(target))
        else:
            await self.page.screenshot(path=str(target), full_page=full_page)
        return ev.relative(target)

    async def content_summary(self, max_chars: int = 20_000) -> dict[str, Any]:
        self._require()
        text = await self.page.inner_text("body")
        html = await self.page.content()
        return {
            "url": self.page.url,
            "title": await self.page.title(),
            "text": text[:max_chars],
            "text_chars": len(text),
            "html_bytes": len(html),
            "element_count": await self.page.evaluate("document.querySelectorAll('*').length"),
        }

    async def accessibility_tree(self, max_nodes: int = 120) -> list[dict[str, Any]]:
        """Prefer Playwright's ARIA snapshot; fall back to a DOM role walk if it is unavailable."""
        self._require()
        try:
            snapshot = await self.page.locator("body").first.aria_snapshot()
            if snapshot:
                lines = [ln for ln in str(snapshot).splitlines() if ln.strip()]
                nodes = []
                for line in lines[:max_nodes]:
                    indent = len(line) - len(line.lstrip())
                    nodes.append({"aria": line.strip()[:160], "depth": indent // 2})
                if nodes:
                    return nodes
        # The ARIA snapshot is optional; fall back to the DOM walk below.
        except Exception:  # nosec B110
            pass
        return await self.page.evaluate(
            """(maxNodes) => {
              const out = [];
              for (const el of document.querySelectorAll('*')) {
                const role = el.getAttribute('role') ||
                  ({A:'link', BUTTON:'button', INPUT:'textbox', SELECT:'combobox',
                    H1:'heading', H2:'heading', H3:'heading', IMG:'img', FORM:'form'})[el.tagName] || null;
                if (!role) continue;
                const name = (el.getAttribute('aria-label') || el.innerText || el.value ||
                              el.getAttribute('alt') || '').trim().slice(0, 80);
                out.push({role, name, depth: 0});
                if (out.length >= maxNodes) break;
              }
              return out;
            }""",
            max_nodes,
        )

    async def a11y_scan(self, include: str | None = None, tags: list[str] | None = None,
                        max_nodes: int = 5) -> dict[str, Any]:
        """Run axe-core against the current document and persist the raw report as evidence."""
        self._require()
        axe = paths.kh_root() / "opt" / "axe" / "axe.min.js"
        if not axe.is_file():
            raise SessionError("axe-core is not installed - run scripts/install_a11y.sh")
        await self.page.add_script_tag(content=axe.read_text(encoding="utf-8"))
        options: dict[str, Any] = {"resultTypes": ["violations"]}
        if tags:
            options["runOnly"] = {"type": "tag", "values": tags}
        if include:
            raw = await self.page.evaluate(
                "async ([ctx, opts]) => await axe.run(ctx, opts)", [{"include": [include]}, options])
        else:
            raw = await self.page.evaluate("async (opts) => await axe.run(document, opts)", options)

        violations = []
        for item in raw.get("violations", []):
            nodes = []
            for node in item.get("nodes", [])[:max_nodes]:
                nodes.append({
                    "target": node.get("target"),
                    "summary": redact_text(str(node.get("failureSummary", "")).splitlines()[0])[:300],
                })
            violations.append({
                "id": item.get("id"),
                "impact": item.get("impact"),
                "help": item.get("help"),
                "help_url": item.get("helpUrl"),
                "tags": item.get("tags", []),
                "node_count": len(item.get("nodes", [])),
                "nodes": nodes,
            })
        folder = paths.evidence_dir("browser", self.run_id or "adhoc", "a11y")
        written = ev.write_json(folder, "axe.json", {
            "url": self.page.url,
            "engine": self.engine,
            "violations": violations,
            "passes": len(raw.get("passes", [])),
            "incomplete": len(raw.get("incomplete", [])),
        })
        return {
            "url": self.page.url,
            "violation_count": len(violations),
            "by_impact": {
                level: sum(1 for v in violations if v["impact"] == level)
                for level in ("critical", "serious", "moderate", "minor")
            },
            "violations": violations,
            "evidence": ev.relative(written),
        }

    async def act(self, action: str, selector: str | None = None, value: str | None = None,
                  timeout_ms: int = 10_000) -> dict[str, Any]:
        self._require()
        allowed = {"click", "fill", "press", "hover", "select", "check", "uncheck", "wait_for_selector",
                   "scroll", "dblclick", "focus", "type", "navigate_back", "navigate_forward", "reload"}
        if action not in allowed:
            raise SessionError(f"unknown action '{action}'; allowed: {', '.join(sorted(allowed))}")
        started = time.monotonic()
        if action in ("navigate_back",):
            await self.page.go_back(wait_until="domcontentloaded")
        elif action in ("navigate_forward",):
            await self.page.go_forward(wait_until="domcontentloaded")
        elif action == "reload":
            await self.page.reload(wait_until="domcontentloaded")
        else:
            if not selector:
                raise SessionError(f"action '{action}' needs a selector")
            locator = self.page.locator(selector).first
            if action == "click":
                await locator.click(timeout=timeout_ms)
            elif action == "dblclick":
                await locator.dblclick(timeout=timeout_ms)
            elif action == "fill":
                await locator.fill(value or "", timeout=timeout_ms)
            elif action == "type":
                await locator.press_sequentially(value or "", timeout=timeout_ms)
            elif action == "press":
                await locator.press(value or "Enter", timeout=timeout_ms)
            elif action == "hover":
                await locator.hover(timeout=timeout_ms)
            elif action == "focus":
                await locator.focus(timeout=timeout_ms)
            elif action == "select":
                await locator.select_option(value or "")
            elif action == "check":
                await locator.check(timeout=timeout_ms)
            elif action == "uncheck":
                await locator.uncheck(timeout=timeout_ms)
            elif action == "scroll":
                await locator.scroll_into_view_if_needed(timeout=timeout_ms)
            elif action == "wait_for_selector":
                await locator.wait_for(timeout=timeout_ms)
        return {"action": action, "selector": selector,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "url_after": self.page.url}

    async def evaluate(self, expression: str, max_chars: int = 5_000) -> Any:
        self._require()
        result = await self.page.evaluate(expression)
        text = str(result)
        return text[:max_chars]

    async def save_auth_state(self, name: str) -> str:
        self._require()
        target = paths.evidence_dir("browser", "auth") / f"{ev.slugify(name, 60)}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(target))
        target.chmod(0o600)
        return ev.relative(target)

    async def network_summary(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.requests[-limit:]

    async def console_summary(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.console[-limit:]


SESSION = BrowserSession()
