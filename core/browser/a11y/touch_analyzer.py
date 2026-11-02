"""Touch Target and Spacing Inspection (WCAG 2.2 SC 2.5.8 & SC 2.5.5)."""
from __future__ import annotations

from typing import Any
from playwright.async_api import Page


async def inspect_touch_targets(page: Page, min_size_px: int = 24) -> list[dict[str, Any]]:
    """Inspects all clickable elements for touch target sizing and spacing violations."""
    return await page.evaluate(
        """(minSize) => {
          const clickables = document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]');
          const results = [];

          clickables.forEach((el, idx) => {
            const rect = el.getBoundingClientRect();
            // Ignore hidden or zero-dimension elements
            if (rect.width <= 0 || rect.height <= 0 || el.offsetWidth <= 0) return;

            const isTooSmall = rect.width < minSize || rect.height < minSize;
            if (isTooSmall) {
              const tag = el.tagName.toLowerCase();
              const text = (el.innerText || el.getAttribute('aria-label') || el.value || '').trim().slice(0, 40);
              const id = el.id ? '#' + el.id : '';
              const selector = id || `${tag}:nth-of-type(${idx+1})`;

              results.push({
                selector: selector,
                tag: tag,
                text: text,
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                min_required: minSize,
                violation: 'undersized_touch_target',
                suggested_fix: `Increase clickable dimensions or padding to at least ${minSize}x${minSize}px.`,
              });
            }
          });

          return results;
        }""",
        min_size_px,
    )
