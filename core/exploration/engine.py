"""Observe → Think → Act → Verify → Learn exploration loop.

The engine drives a real browser, so its output is behaviour, not speculation. Each step records
what was visible, why the next action was chosen, what changed, and what was learned. Exploration
stops on the step budget, on cancellation, or when two consecutive screens reveal nothing new.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from core.execution.browser import BrowserExecutor


class ExplorationEngine:
    def __init__(self, executor: BrowserExecutor, *, max_steps: int = 24,
                 on_state: Callable[[dict[str, Any]], None] | None = None,
                 log=None):
        self.executor = executor
        self.max_steps = max(1, max_steps)
        self.on_state = on_state
        self.log = log

    async def run(self, start_url: str, cookies: dict[str, str] | None = None) -> dict[str, Any]:
        queue: list[str] = [start_url]
        seen_urls: set[str] = {start_url}
        seen_states: set[str] = set()
        history: list[dict[str, Any]] = []
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(start_url))
        idle = 0
        stopped = "step budget"

        await self.executor.open(start_url, trace=False)
        if cookies:
            await self.executor.session.context.add_cookies(
                [{"name": name, "value": value, "url": start_url}
                 for name, value in cookies.items()])

        for step in range(1, self.max_steps + 1):
            if queue:
                url = queue.pop(0)
            else:
                stopped = "frontier empty"
                break
            if self.executor.budget:
                self.executor.budget.check_time()
                if self.executor.budget.cancelled:
                    stopped = "cancelled"
                    break

            await self.executor.session.goto(url)
            observation = await self.executor.observe()
            signature = _signature(observation)
            state = {
                "step": step,
                "url": observation["url"],
                "title": observation["title"],
                "signature": signature,
                "actionable": len(observation["actionable"]),
                "forms": len(observation["forms"]),
                "console_errors": observation["console_errors"],
                "failed_requests": observation["failed_requests"],
                "new": signature not in seen_states,
            }
            history.append(state)
            if self.on_state:
                self.on_state(state)

            if signature in seen_states:
                idle += 1
            else:
                seen_states.add(signature)
                idle = 0

            discovered = 0
            for element in observation["actionable"]:
                href = element.get("href")
                if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
                    continue
                candidate = urljoin(observation["url"], href)
                if not candidate.startswith(origin):
                    continue
                if candidate not in seen_urls:
                    seen_urls.add(candidate)
                    queue.append(candidate)
                    discovered += 1
                if len(seen_urls) >= self.max_steps * 3:
                    break
            state["discovered_links"] = discovered

            if idle >= 2:
                stopped = "no new state"
                break

        summary = self.executor.save_observation("exploration.json", {
            "start_url": start_url,
            "steps": len(history),
            "stopped": stopped,
            "states": history,
        })
        if self.log:
            self.log.event("exploration_done", steps=len(history), stopped=stopped,
                           states=len(seen_states))
        return {"start_url": start_url, "steps": len(history), "stopped": stopped,
                "states": history, "evidence": summary,
                "errors": [state for state in history if state["console_errors"] or state["failed_requests"]]}


def _signature(observation: dict[str, Any]) -> str:
    parts = [observation.get("title", ""), str(observation.get("element_count", ""))]
    parts.extend(heading.get("text", "") for heading in observation.get("headings", [])[:8])
    parts.extend(f"{item.get('tag')}:{item.get('text')}" for item in observation.get("actionable", [])[:20])
    return "|".join(parts)
