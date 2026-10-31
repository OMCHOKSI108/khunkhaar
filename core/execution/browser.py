"""Browser helpers for Phase 2 agents.

Thin, typed layer over the Phase 1 BrowserSession. It adds state observation, responsive
checks and evidence capture, and charges every session/step to the run budget.
"""

from __future__ import annotations

from typing import Any

from core.browser.session import BrowserSession
from core.shared import evidence as ev


class BrowserExecutor:
    def __init__(self, session: BrowserSession | None = None, budget=None, log=None):
        self.session = session or BrowserSession()
        self.budget = budget
        self.log = log

    # ---- lifecycle ----------------------------------------------------------
    async def open(self, url: str, **kwargs: Any) -> dict[str, Any]:
        if self.budget:
            self.budget.open_browser_session()
        self.budget and self.budget.consume_steps()
        started = await self.session.start(**kwargs)
        if self.log:
            self.log.event("browser_open", url=url, engine=started.get("engine"),
                           run_id=started.get("run_id"))
        navigation = await self.session.goto(url, timeout_ms=30_000)
        return {"start": started, "navigation": navigation}

    async def close(self) -> dict[str, Any]:
        summary = await self.session.stop()
        if self.log:
            self.log.event("browser_close", run_id=summary.get("run_id"))
        return summary

    @property
    def active(self) -> bool:
        return self.session.active

    # ---- observation --------------------------------------------------------
    async def observe(self) -> dict[str, Any]:
        """Structured snapshot of the page used by the Observe→Think→Act loop."""
        self.budget and self.budget.consume_steps()
        data = await self.session.page.evaluate(
            """() => {
              const cssPath = (el) => {
                if (el.id) return '#' + CSS.escape(el.id);
                for (const attr of ['data-testid', 'name']) {
                  const v = el.getAttribute(attr);
                  if (v) return el.tagName.toLowerCase() + '[' + attr + '="' + CSS.escape(v) + '"]';
                }
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && parts.length < 4) {
                  let index = 1, sib = node;
                  while ((sib = sib.previousElementSibling)) {
                    if (sib.tagName === node.tagName) index++;
                  }
                  parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
                  node = node.parentElement;
                }
                return parts.join(' > ');
              };
              const describe = (el, i) => ({
                index: i,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || null,
                name: el.getAttribute('name') || el.getAttribute('data-testid') || null,
                text: (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 80),
                href: el.getAttribute('href') || null,
                disabled: !!el.disabled,
                selector: cssPath(el),
              });
              const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const actionable = [...document.querySelectorAll(
                'a,button,input,select,textarea,[role=button],[onclick]'
              )].filter(visible).slice(0, 150).map(describe);
              const headings = [...document.querySelectorAll('h1,h2,h3')].slice(0, 30)
                .map(h => ({level: h.tagName, text: h.innerText.trim().slice(0, 120)}));
              const forms = [...document.forms].slice(0, 20).map(f => ({
                action: f.getAttribute('action') || location.pathname,
                method: (f.getAttribute('method') || 'get').toLowerCase(),
                fields: [...f.elements].filter(e => e.name).map(e => ({
                  name: e.name, type: e.type, required: !!e.required
                })).slice(0, 40),
              }));
              return {
                url: location.href,
                title: document.title,
                text: document.body.innerText.slice(0, 4000),
                headings, forms, actionable,
                element_count: document.querySelectorAll('*').length,
                scroll_width: document.documentElement.scrollWidth,
                client_width: document.documentElement.clientWidth,
                inner_width: window.innerWidth,
              };
            }"""
        )
        data["console_errors"] = [c for c in self.session.console if c["type"] in ("error", "pageerror")][-10:]
        data["failed_requests"] = [r for r in self.session.requests if (r.get("status") or 0) >= 400][-10:]
        return data

    async def responsive_report(self) -> dict[str, Any]:
        """Overflow check that names offending elements without judging visual taste."""
        self.budget and self.budget.consume_steps()
        return await self.session.page.evaluate(
            """() => {
              // documentElement.clientWidth stays at the real emulated viewport, while
              // window.innerWidth can inflate when fixed-width content expands the layout
              // viewport under a mobile meta tag. Overflow must be measured against the former.
              const vw = document.documentElement.clientWidth;
              const offenders = [];
              for (const el of document.querySelectorAll('body *')) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && (r.right > vw + 1 || r.width > vw + 1)) {
                  offenders.push({
                    tag: el.tagName.toLowerCase(),
                    cls: (el.className || '').toString().slice(0, 60),
                    right: Math.round(r.right), width: Math.round(r.width),
                  });
                  if (offenders.length >= 10) break;
                }
              }
              return {
                url: location.href,
                inner_width: vw,
                scroll_width: document.documentElement.scrollWidth,
                horizontal_overflow: document.documentElement.scrollWidth > vw + 1,
                offenders,
              };
            }"""
        )

    async def screenshot(self, name: str) -> str:
        return await self.session.screenshot(name)

    async def a11y(self) -> dict[str, Any]:
        return await self.session.a11y_scan()

    async def evaluate(self, expression: str) -> Any:
        return await self.session.evaluate(expression)

    async def act(self, action: str, selector: str | None = None,
                  value: str | None = None) -> dict[str, Any]:
        self.budget and self.budget.consume_steps()
        result = await self.session.act(action, selector=selector, value=value)
        if self.log:
            self.log.event("browser_action", action=action, selector=selector,
                           url_after=result.get("url_after"))
        return result

    def save_observation(self, name: str, observation: dict[str, Any]) -> str:
        folder = ev.evidence_root("browser", self.session.run_id or "phase2")
        path = ev.write_json(folder, name, observation)
        return ev.relative(path)
