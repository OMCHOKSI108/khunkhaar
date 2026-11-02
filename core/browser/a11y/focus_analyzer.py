"""Focus Management and Directed Focus Graph Analyzer."""
from __future__ import annotations

import asyncio
from typing import Any
from playwright.async_api import Page

from core.browser.a11y.keyboard_engine import get_active_element_info, inspect_tab_sequence


def build_focus_graph(sequence: list[dict[str, Any]]) -> dict[str, Any]:
    """Builds a directed graph representing keyboard focus transitions."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    cycles: list[list[str]] = []

    for i in range(len(sequence)):
        curr = sequence[i]
        curr_key = curr.get("element_key") or f"{curr['tag']}-{i}"
        if curr_key not in nodes:
            nodes[curr_key] = {
                "tag": curr["tag"], "role": curr["role"], "name": curr["name"],
                "has_visible_focus": curr.get("has_visible_focus", True),
            }
        if i > 0:
            prev = sequence[i - 1]
            prev_key = prev.get("element_key") or f"{prev['tag']}-{i-1}"
            edges.append({"from": prev_key, "to": curr_key})
            if curr_key == sequence[0].get("element_key") and i > 2:
                cycles.append([prev_key, curr_key])

    return {
        "node_count": len(nodes),
        "nodes": nodes,
        "edge_count": len(edges),
        "edges": edges,
        "has_cycle": len(cycles) > 0,
        "cycles": cycles,
    }


async def test_modal_focus_trap(page: Page, modal_selector: str = '[role="dialog"]') -> dict[str, Any]:
    """Verifies that when a modal dialog is open, focus cannot escape into background DOM."""
    is_open = await page.evaluate(f"() => !!document.querySelector('{modal_selector}')")
    if not is_open:
        return {"modal_found": False, "trapped": True, "escaped_elements": []}

    escaped: list[dict[str, Any]] = []
    for _ in range(15):
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.05)
        inside = await page.evaluate(
            f"""() => {{
              const active = document.activeElement;
              const modal = document.querySelector('{modal_selector}');
              if (!active || !modal) return true;
              return modal.contains(active);
            }}"""
        )
        if not inside:
            info = await get_active_element_info(page)
            if info:
                escaped.append(info)

    return {
        "modal_found": True,
        "trapped": len(escaped) == 0,
        "escaped_elements": escaped,
    }


async def test_focus_restoration(page: Page, trigger_selector: str, close_selector: str) -> dict[str, Any]:
    """Tests if focus returns to the triggering control after closing a modal or popup."""
    # 1. Focus and activate trigger
    await page.focus(trigger_selector)
    before_info = await get_active_element_info(page)

    # 2. Open dialog
    await page.click(trigger_selector)
    await asyncio.sleep(0.2)

    # 3. Close dialog
    await page.click(close_selector)
    await asyncio.sleep(0.2)

    # 4. Check active element after close
    after_info = await get_active_element_info(page)
    restored = False
    if before_info and after_info:
        restored = (before_info.get("id") == after_info.get("id")) or (before_info.get("name") == after_info.get("name"))

    return {
        "trigger_before": before_info,
        "focus_after_close": after_info,
        "restored": restored,
    }
