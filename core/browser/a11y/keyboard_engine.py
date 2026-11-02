"""Keyboard Navigation Engine: drives Tab/Shift+Tab, Enter, Space, and Escape."""
from __future__ import annotations

import asyncio
from typing import Any
from playwright.async_api import Page


async def get_active_element_info(page: Page) -> dict[str, Any] | None:
    return await page.evaluate(
        """() => {
          const el = document.activeElement;
          if (!el || el === document.body || el === document.documentElement) return null;
          const s = window.getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const hasOutline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth || '0') > 0;
          const hasBoxShadow = s.boxShadow !== 'none' && s.boxShadow.length > 0;
          const hasVisibleIndicator = hasOutline || hasBoxShadow || el.getAttribute('data-focus-visible') === 'true';
          return {
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            name: el.getAttribute('aria-label') || el.getAttribute('name') || el.innerText?.slice(0, 80) || '',
            id: el.id || null,
            visible: r.width > 0 && r.height > 0,
            has_visible_focus: hasVisibleIndicator,
            outline: s.outlineStyle,
            box_shadow: s.boxShadow,
            bounding_box: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)},
          };
        }"""
    )


async def inspect_tab_sequence(page: Page, max_tabs: int = 50) -> list[dict[str, Any]]:
    """Presses Tab repeatedly and records the observed interactive focus sequence."""
    sequence: list[dict[str, Any]] = []
    seen_elements: set[str] = set()

    # Reset focus to top
    await page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")

    for step in range(max_tabs):
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.05)
        info = await get_active_element_info(page)
        if not info:
            continue
        key = f"{info['tag']}#{info['id'] or ''}.{info['role']}[{info['name'][:20]}]"
        info["step"] = step + 1
        info["element_key"] = key
        sequence.append(info)

        # Early break if looping back to the very first item multiple times
        if len(sequence) > 3 and key == sequence[0]["element_key"]:
            break

    return sequence


async def test_escape_dismissal(page: Page, trigger_selector: str | None = None) -> dict[str, Any]:
    """Tests whether pressing Escape dismisses an active dialog, menu, or popup."""
    if trigger_selector:
        await page.click(trigger_selector)
        await asyncio.sleep(0.1)

    before = await page.evaluate(
        """() => {
          const modals = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], .modal.show, .popup-active');
          return Array.from(modals).filter(m => m.offsetWidth > 0 && m.offsetHeight > 0).length;
        }"""
    )

    await page.keyboard.press("Escape")
    await asyncio.sleep(0.15)

    after = await page.evaluate(
        """() => {
          const modals = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], .modal.show, .popup-active');
          return Array.from(modals).filter(m => m.offsetWidth > 0 && m.offsetHeight > 0).length;
        }"""
    )

    dismissed = before > 0 and after < before
    return {
        "modal_detected_before": before > 0,
        "modal_count_before": before,
        "modal_count_after": after,
        "dismissed": dismissed,
    }


async def test_reverse_tab_sequence(page: Page, steps: int = 5) -> list[dict[str, Any]]:
    """Presses Shift+Tab to verify reverse keyboard navigation works smoothly."""
    rev_sequence: list[dict[str, Any]] = []
    for step in range(steps):
        await page.keyboard.press("Shift+Tab")
        await asyncio.sleep(0.05)
        info = await get_active_element_info(page)
        if info:
            info["step"] = step + 1
            rev_sequence.append(info)
    return rev_sequence
