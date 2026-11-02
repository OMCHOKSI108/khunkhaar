"""MCP Tool Implementations for Accessibility & UX Intelligence Engine v2."""
from __future__ import annotations

from typing import Any
from core.browser.a11y import (
    focus_analyzer,
    form_analyzer,
    journey_engine,
    keyboard_engine,
    orchestrator,
    responsive_engine,
    visual_compare,
)
from core.browser.session import SESSION, SessionError
from core.shared import paths


def _require():
    if not SESSION.active or not SESSION.page:
        raise SessionError("No active browser session - call browser_open first")


async def browser_keyboard_inspect(max_tabs: int = 30) -> dict[str, Any]:
    _require()
    seq = await keyboard_engine.inspect_tab_sequence(SESSION.page, max_tabs=max_tabs)
    return {"ok": True, "count": len(seq), "sequence": seq}


async def browser_focus_inspect(max_tabs: int = 30) -> dict[str, Any]:
    _require()
    seq = await keyboard_engine.inspect_tab_sequence(SESSION.page, max_tabs=max_tabs)
    graph = focus_analyzer.build_focus_graph(seq)
    return {"ok": True, **graph}


async def browser_form_inspect() -> dict[str, Any]:
    _require()
    fields = await form_analyzer.inspect_form_accessibility(SESSION.page)
    defects = form_analyzer.summarize_form_defects(fields)
    return {"ok": True, "field_count": len(fields), "fields": fields, "defects": defects}


async def browser_responsive_inspect(viewport: str = "mobile") -> dict[str, Any]:
    _require()
    res = await responsive_engine.inspect_viewport_layout(SESSION.page, viewport_name=viewport)
    return {"ok": True, **res}


async def browser_visual_compare(baseline_name: str, threshold: float = 0.02) -> dict[str, Any]:
    _require()
    base_dir = paths.evidence_dir("browser", "baselines")
    base_path = base_dir / f"{baseline_name}.png"
    diff_path = paths.evidence_dir("browser", SESSION.run_id or "adhoc", "visual") / f"diff_{baseline_name}.png"
    res = await visual_compare.compare_page_with_baseline(SESSION.page, base_path, diff_path, threshold=threshold)
    return {"ok": True, **res}


async def browser_journey_execute(journey_name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    _require()
    res = await journey_engine.execute_journey(SESSION.page, journey_name, steps)
    return {
        "ok": True,
        "journey_name": res.journey_name,
        "success": res.success,
        "steps_total": res.steps_total,
        "steps_completed": res.steps_completed,
        "failed_step_index": res.failed_step_index,
        "error_message": res.error_message,
        "steps": res.step_records,
    }


async def browser_ux_report(profile: str = "wcag_22_aa") -> dict[str, Any]:
    _require()
    report = await orchestrator.run_full_ux_inspection(SESSION.page, profile=profile)
    return {"ok": True, "report": report}
