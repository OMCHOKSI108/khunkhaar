"""User Journey & Usability Execution Engine."""
from __future__ import annotations

import asyncio
from typing import Any
from playwright.async_api import Page

from core.browser.a11y.models import JourneyResult, JourneyStep


async def resolve_target_locator(page: Page, target: str | dict[str, str]):
    if isinstance(target, dict):
        role = target.get("role")
        name = target.get("name")
        if role and name:
            return page.get_by_role(role, name=name)
        if name:
            return page.get_by_text(name)
        if role:
            return page.get_by_role(role)
    return page.locator(str(target))


async def evaluate_assertion(page: Page, assertion: str) -> tuple[bool, str]:
    if not assertion:
        return True, "No assertion specified"
    op, _, val = assertion.partition(":")
    val = val.strip()

    if op == "url_contains":
        cur = page.url
        return val in cur, f"URL '{cur}' contains '{val}'"
    if op == "text_present":
        found = await page.evaluate(f"(t) => document.body.innerText.includes(t)", val)
        return bool(found), f"Text '{val}' in page body"
    if op == "visible":
        loc = await resolve_target_locator(page, val)
        is_vis = await loc.is_visible()
        return is_vis, f"Element '{val}' visible: {is_vis}"
    if op == "not_visible":
        loc = await resolve_target_locator(page, val)
        is_vis = await loc.is_visible()
        return not is_vis, f"Element '{val}' not visible: {not is_vis}"

    return True, f"Unknown assertion operator '{op}' passed"


async def execute_journey(page: Page, journey_name: str, steps: list[JourneyStep | dict[str, Any]]) -> JourneyResult:
    records = []
    completed = 0

    for idx, raw_step in enumerate(steps):
        step = raw_step if isinstance(raw_step, JourneyStep) else JourneyStep(**raw_step)
        action = step.action.lower()
        step_rec = {"step_index": idx + 1, "action": action, "target": str(step.target), "success": False}

        try:
            if action == "navigate":
                await page.goto(str(step.target), wait_until="domcontentloaded", timeout=15000)
            elif action == "click":
                loc = await resolve_target_locator(page, step.target)
                await loc.click(timeout=10000)
            elif action == "fill":
                loc = await resolve_target_locator(page, step.target)
                await loc.fill(step.value or "", timeout=10000)
            elif action == "select":
                loc = await resolve_target_locator(page, step.target)
                await loc.select_option(step.value or "")
            elif action == "press":
                await page.keyboard.press(step.value or "Enter")
            elif action == "wait":
                await asyncio.sleep(float(step.value or 1.0))

            # Evaluate step assertion
            assert_ok = True
            msg = ""
            if step.assertion:
                assert_ok, msg = await evaluate_assertion(page, step.assertion)

            step_rec["success"] = assert_ok
            step_rec["assertion_result"] = msg
            records.append(step_rec)

            if not assert_ok:
                return JourneyResult(
                    journey_name=journey_name,
                    success=False,
                    steps_total=len(steps),
                    steps_completed=completed,
                    failed_step_index=idx + 1,
                    error_message=f"Step {idx + 1} assertion failed: {msg}",
                    step_records=records,
                )

            completed += 1
        except Exception as exc:
            step_rec["error"] = str(exc)
            records.append(step_rec)
            return JourneyResult(
                journey_name=journey_name,
                success=False,
                steps_total=len(steps),
                steps_completed=completed,
                failed_step_index=idx + 1,
                error_message=f"Step {idx + 1} action '{action}' failed: {exc}",
                step_records=records,
            )

    return JourneyResult(
        journey_name=journey_name,
        success=True,
        steps_total=len(steps),
        steps_completed=completed,
        step_records=records,
    )
