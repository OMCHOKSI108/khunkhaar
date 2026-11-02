"""Accessibility & UX Intelligence Engine v2 Package."""
from __future__ import annotations

from core.browser.a11y.focus_analyzer import build_focus_graph, test_focus_restoration, test_modal_focus_trap
from core.browser.a11y.form_analyzer import inspect_form_accessibility, summarize_form_defects
from core.browser.a11y.issue_tracker import IssueTracker, calculate_severity, compute_issue_fingerprint
from core.browser.a11y.journey_engine import execute_journey
from core.browser.a11y.keyboard_engine import get_active_element_info, inspect_tab_sequence, test_escape_dismissal
from core.browser.a11y.models import FindingKind, IssueCategory, IssueStatus, JourneyResult, JourneyStep, Severity, UXIssue
from core.browser.a11y.orchestrator import run_full_ux_inspection
from core.browser.a11y.responsive_engine import VIEWPORT_MATRIX, inspect_viewport_layout
from core.browser.a11y.touch_analyzer import inspect_touch_targets
from core.browser.a11y.visual_compare import compare_page_with_baseline, compute_image_hash
from core.browser.a11y.wcag_profiles import WCAG_TAG_PROFILES, get_axe_asset_metadata, get_wcag_tags


__all__ = [
    "IssueCategory",
    "Severity",
    "IssueStatus",
    "FindingKind",
    "UXIssue",
    "JourneyStep",
    "JourneyResult",
    "WCAG_TAG_PROFILES",
    "get_wcag_tags",
    "get_axe_asset_metadata",
    "inspect_tab_sequence",
    "get_active_element_info",
    "test_escape_dismissal",
    "build_focus_graph",
    "test_modal_focus_trap",
    "test_focus_restoration",
    "inspect_form_accessibility",
    "summarize_form_defects",
    "inspect_touch_targets",
    "VIEWPORT_MATRIX",
    "inspect_viewport_layout",
    "compare_page_with_baseline",
    "execute_journey",
    "IssueTracker",
    "compute_issue_fingerprint",
    "calculate_severity",
    "run_full_ux_inspection",
]
