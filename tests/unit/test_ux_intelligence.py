"""Unit tests for Accessibility & UX Intelligence Engine v2."""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from core.browser.a11y import (
    IssueCategory,
    IssueStatus,
    FindingKind,
    JourneyStep,
    Severity,
    UXIssue,
    build_focus_graph,
    calculate_severity,
    compute_image_hash,
    compute_issue_fingerprint,
    get_axe_asset_metadata,
    get_wcag_tags,
    summarize_form_defects,
    IssueTracker,
    VIEWPORT_MATRIX,
)


def test_ux_issue_to_dict():
    issue = UXIssue(
        issue_id="ISS-TEST001",
        category=IssueCategory.KEYBOARD,
        subcategory="focus_trap",
        title="Modal dialog traps focus indefinitely",
        severity=Severity.HIGH,
        confidence=0.95,
        status=IssueStatus.NEW,
        url="http://localhost:8731/checkout",
        selector="#payment-modal",
        wcag_criterion="2.1.2",
        wcag_level="A",
        finding_kind=FindingKind.INTERACTION_FAILURE,
        reproduction=["Open modal", "Press Tab 15 times"],
        suggested_fix="Implement focus wrap around or close on escape",
    )
    d = issue.to_dict()
    assert d["issue_id"] == "ISS-TEST001"
    assert d["category"] == "keyboard"
    assert d["severity"] == "high"
    assert d["wcag"]["criterion"] == "2.1.2"
    assert d["confidence"] == 0.95


def test_wcag_profiles_and_asset_metadata():
    tags_22 = get_wcag_tags("wcag_22_aa")
    assert "wcag22aa" in tags_22
    assert "wcag2a" in tags_22

    tags_bp = get_wcag_tags("best_practice")
    assert tags_bp == ["best-practice"]

    meta = get_axe_asset_metadata()
    assert meta["installed"] is True
    assert meta["sha256"] is not None
    assert meta["file_size"] > 100_000


def test_focus_graph_cycle_detection():
    # Sequence with a loop back to item 1
    seq = [
        {"tag": "input", "role": "textbox", "name": "Email", "element_key": "input#email", "has_visible_focus": True},
        {"tag": "input", "role": "textbox", "name": "Pass", "element_key": "input#pass", "has_visible_focus": True},
        {"tag": "button", "role": "button", "name": "Submit", "element_key": "btn#submit", "has_visible_focus": False},
        {"tag": "input", "role": "textbox", "name": "Email", "element_key": "input#email", "has_visible_focus": True},
    ]
    graph = build_focus_graph(seq)
    assert graph["node_count"] == 3
    assert graph["edge_count"] == 3
    assert graph["has_cycle"] is True
    assert graph["nodes"]["btn#submit"]["has_visible_focus"] is False


def test_form_analyzer_defects():
    sample_controls = [
        {"id": "user", "type": "text", "has_accessible_name": False, "only_placeholder_as_label": False, "selector": "#user"},
        {"id": "search", "type": "search", "has_accessible_name": False, "only_placeholder_as_label": True, "selector": "#search"},
        {"id": "pwd", "type": "password", "has_accessible_name": True, "only_placeholder_as_label": False, "selector": "#pwd"},
    ]
    defects = summarize_form_defects(sample_controls)
    assert len(defects) == 2
    types = [d["type"] for d in defects]
    assert "missing_form_label" in types
    assert "placeholder_only_label" in types


def test_viewport_matrix():
    assert "small_mobile" in VIEWPORT_MATRIX
    assert VIEWPORT_MATRIX["small_mobile"]["width"] == 320
    assert VIEWPORT_MATRIX["large_desktop"]["width"] == 1920


def test_image_hash():
    h1 = compute_image_hash(b"test_image_bytes")
    h2 = compute_image_hash(b"test_image_bytes")
    h3 = compute_image_hash(b"different_bytes")
    assert h1 == h2
    assert h1 != h3


def test_issue_tracker_lifecycle(tmp_path: Path):
    store_file = tmp_path / "issues.json"
    tracker = IssueTracker(store_file)

    fid = compute_issue_fingerprint("forms", "missing_label", "#name", "Missing label")
    iss = UXIssue(
        issue_id=fid, category=IssueCategory.FORMS, subcategory="missing_label",
        title="Missing label", severity=Severity.HIGH, confidence=0.9,
        status=IssueStatus.NEW, url="http://test.local"
    )

    # 1. New issue
    tracked = tracker.record_issue(iss)
    assert tracked.status == IssueStatus.NEW

    # 2. Existing issue on second run
    tracked2 = tracker.record_issue(iss)
    assert tracked2.status == IssueStatus.EXISTING

    # 3. Mark fixed when absent from active run
    fixed = tracker.mark_fixed_if_absent(set())
    assert fid in fixed
    assert tracker.get_issues()[0].status == IssueStatus.FIXED

    # 4. Regressed issue if it appears again
    tracked4 = tracker.record_issue(iss)
    assert tracked4.status == IssueStatus.REGRESSED
