"""Responsive Layout & Visual Defect Engine across a standardized viewport matrix."""
from __future__ import annotations

from typing import Any
from playwright.async_api import Page

VIEWPORT_MATRIX: dict[str, dict[str, int]] = {
    "small_mobile": {"width": 320, "height": 700},
    "mobile": {"width": 375, "height": 812},
    "large_mobile": {"width": 430, "height": 932},
    "tablet": {"width": 768, "height": 1024},
    "laptop": {"width": 1366, "height": 768},
    "large_desktop": {"width": 1920, "height": 1080},
}


async def inspect_viewport_layout(page: Page, viewport_name: str = "mobile") -> dict[str, Any]:
    """Inspects layout on the current viewport for overflow, clipping, overlaps, and obstructions."""
    vp = VIEWPORT_MATRIX.get(viewport_name, VIEWPORT_MATRIX["mobile"])
    await page.set_viewport_size({"width": vp["width"], "height": vp["height"]})

    return await page.evaluate(
        """(vpConfig) => {
          const vw = document.documentElement.clientWidth;
          const vh = document.documentElement.clientHeight;
          const sw = document.documentElement.scrollWidth;
          const sh = document.documentElement.scrollHeight;

          const defects = [];

          // 1. Horizontal page overflow
          if (sw > vw + 1) {
            defects.push({
              issue: 'horizontal_page_overflow',
              severity: 'high',
              details: `Page scrollWidth (${sw}px) exceeds viewport width (${vw}px) by ${sw - vw}px.`,
            });
          }

          // 2. Offscreen or clipped elements
          const elements = document.querySelectorAll('body *');
          const interactive = [];

          for (const el of elements) {
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;

            // Element overflowing right
            if (r.right > vw + 2) {
              const tag = el.tagName.toLowerCase();
              const cls = (el.className || '').toString().slice(0, 40);
              defects.push({
                issue: 'element_viewport_overflow',
                severity: 'medium',
                tag: tag,
                className: cls,
                right: Math.round(r.right),
                viewport_width: vw,
                overflow_px: Math.round(r.right - vw),
              });
              if (defects.length >= 10) break;
            }

            // Track interactive elements for overlap check
            if (['BUTTON', 'A', 'INPUT'].includes(el.tagName)) {
              interactive.push({el, r, text: (el.innerText || el.value || '').trim().slice(0, 30)});
            }
          }

          // 3. Overlapping interactive controls
          for (let i = 0; i < Math.min(interactive.length, 25); i++) {
            for (let j = i + 1; j < Math.min(interactive.length, 25); j++) {
              const a = interactive[i].r;
              const b = interactive[j].r;
              const overlaps = !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
              if (overlaps && interactive[i].el !== interactive[j].el && !interactive[i].el.contains(interactive[j].el)) {
                defects.push({
                  issue: 'overlapping_interactive_elements',
                  severity: 'high',
                  first: interactive[i].text || interactive[i].el.tagName,
                  second: interactive[j].text || interactive[j].el.tagName,
                });
                break;
              }
            }
          }

          return {
            viewport: vpConfig,
            horizontal_overflow: sw > vw + 1,
            scroll_width: sw,
            viewport_width: vw,
            defect_count: defects.length,
            defects: defects,
          };
        }""",
        vp,
    )
