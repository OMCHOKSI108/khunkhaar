"""Inspection Orchestrator: coordinates accessibility, keyboard, forms, touch, and responsive engines."""
from __future__ import annotations

import time
from typing import Any
from playwright.async_api import Page

from core.browser.a11y import focus_analyzer, form_analyzer, keyboard_engine, responsive_engine, touch_analyzer, wcag_profiles
from core.browser.a11y.issue_tracker import IssueTracker, calculate_severity, compute_issue_fingerprint
from core.browser.a11y.models import FindingKind, IssueCategory, IssueStatus, UXIssue


async def run_full_ux_inspection(page: Page, profile: str = "wcag_22_aa", tracker: IssueTracker | None = None) -> dict[str, Any]:
    started_at = time.time()
    url = page.url
    issues: list[UXIssue] = []

    # 1. Keyboard Navigation Sequence
    seq = await keyboard_engine.inspect_tab_sequence(page, max_tabs=30)
    focus_graph = focus_analyzer.build_focus_graph(seq)

    # Missing focus indicators
    for node_key, node in focus_graph["nodes"].items():
        if not node["has_visible_focus"]:
            fid = compute_issue_fingerprint("focus", "visible_focus", node_key, "Element lacks visible focus indicator")
            iss = UXIssue(
                issue_id=fid, category=IssueCategory.FOCUS, subcategory="focus_visibility",
                title=f"Interactive element <{node['tag']}> lacks visible focus indicator",
                severity=calculate_severity(IssueCategory.FOCUS, FindingKind.INTERACTION_FAILURE),
                confidence=0.92, status=IssueStatus.NEW, finding_kind=FindingKind.INTERACTION_FAILURE,
                url=url, selector=node_key, suggested_fix="Ensure :focus or :focus-visible provides a distinct outline or contrast change.",
            )
            issues.append(iss)

    # 2. Form Accessibility
    form_results = await form_analyzer.inspect_form_accessibility(page)
    form_defects = form_analyzer.summarize_form_defects(form_results)
    for defct in form_defects:
        fid = compute_issue_fingerprint("forms", defct["type"], defct["selector"], defct["message"])
        iss = UXIssue(
            issue_id=fid, category=IssueCategory.FORMS, subcategory=defct["type"],
            title=defct["message"],
            severity=calculate_severity(IssueCategory.FORMS, FindingKind.AUTOMATED_VIOLATION),
            confidence=0.95, status=IssueStatus.NEW, finding_kind=FindingKind.AUTOMATED_VIOLATION,
            url=url, selector=defct["selector"], suggested_fix=defct["suggested_fix"],
        )
        issues.append(iss)

    # 3. Touch Target Sizing (WCAG 2.2 SC 2.5.8)
    touch_violations = await touch_analyzer.inspect_touch_targets(page, min_size_px=24)
    for tv in touch_violations:
        fid = compute_issue_fingerprint("accessibility", "touch_target", tv["selector"], tv["violation"])
        iss = UXIssue(
            issue_id=fid, category=IssueCategory.ACCESSIBILITY, subcategory="touch_target",
            title=f"Clickable <{tv['tag']}> target size ({tv['width']}x{tv['height']}px) below minimum 24x24px",
            severity=calculate_severity(IssueCategory.ACCESSIBILITY, FindingKind.AUTOMATED_VIOLATION),
            confidence=0.98, status=IssueStatus.NEW, finding_kind=FindingKind.AUTOMATED_VIOLATION,
            url=url, selector=tv["selector"], wcag_criterion="2.5.8", wcag_level="AA",
            suggested_fix=tv["suggested_fix"],
        )
        issues.append(iss)

    # 4. Responsive Layout Checks
    resp = await responsive_engine.inspect_viewport_layout(page, viewport_name="mobile")
    for defct in resp.get("defects", []):
        fid = compute_issue_fingerprint("responsive", defct["issue"], None, str(defct))
        iss = UXIssue(
            issue_id=fid, category=IssueCategory.RESPONSIVE, subcategory=defct["issue"],
            title=f"Responsive layout defect: {defct['issue']}",
            severity=calculate_severity(IssueCategory.RESPONSIVE, FindingKind.INTERACTION_FAILURE),
            confidence=0.90, status=IssueStatus.NEW, finding_kind=FindingKind.INTERACTION_FAILURE,
            url=url, suggested_fix="Adjust responsive CSS breakpoint bounds and max-width sizing.",
        )
        issues.append(iss)

    # Record in tracker if present
    if tracker:
        active_ids = {i.issue_id for i in issues}
        for iss in issues:
            tracker.record_issue(iss)
        tracker.mark_fixed_if_absent(active_ids)

    return {
        "url": url,
        "wcag_profile": profile,
        "inspection_duration_ms": int((time.time() - started_at) * 1000),
        "keyboard_steps": len(seq),
        "focus_nodes_inspected": focus_graph["node_count"],
        "forms_inspected": len(form_results),
        "touch_targets_flagged": len(touch_violations),
        "responsive_defects": resp["defect_count"],
        "total_issues_found": len(issues),
        "issues": [i.to_dict() for i in issues],
    }
