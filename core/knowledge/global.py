"""Technique-level knowledge that is safe to reuse across every project.

This library is curated in code, not derived from a repository. It answers "how should this class
of defect be probed?" and deliberately contains no project id, repository path, rule text, finding
title or target address. `save()` writes it under `intelligence/global-knowledge/`; a test greps
the written artifact for project identifiers to keep that boundary enforced.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from core.metadata import store
from core.shared import evidence, paths

SCHEMA = 1

TECHNIQUES: tuple[dict[str, Any], ...] = (
    {
        "id": "GT-CROSS-USER-ACCESS",
        "title": "Cross-user resource access probe",
        "categories": ["authorization", "security"],
        "technique": ("Create a resource as the owner, capture its identifier, then request it as a "
                      "second user who does not own it."),
        "expected": "A non-owner receives 403 or 404; a 2xx that returns the owner's data is a "
                    "broken permission boundary.",
        "evidence_hint": "Record the identifier, the second role, the status and a short body "
                         "excerpt; never record credentials.",
        "limitations": "A test with one account cannot distinguish ownership from authentication.",
    },
    {
        "id": "GT-INVALID-INPUT",
        "title": "Invalid and boundary input probe",
        "categories": ["validation", "api"],
        "technique": ("Submit empty required text, non-positive numbers, over-length values and "
                      "type-mismatched values, then read the resource back."),
        "expected": "The API rejects invalid values with 4xx and does not persist the request.",
        "evidence_hint": "Keep the request shape and status; redact the body before storage.",
        "limitations": "A 4xx without reading the resource back does not prove nothing was stored.",
    },
    {
        "id": "GT-REPEATED-SUBMISSION",
        "title": "Repeated identical submission probe",
        "categories": ["idempotency", "reliability"],
        "technique": "Send the same create request twice and count the resulting records.",
        "expected": "Either one record exists or the second response is an explicit conflict.",
        "evidence_hint": "Record both statuses and the observed record count.",
        "limitations": "A database count is only available when a read-only database profile is "
                       "supplied.",
    },
    {
        "id": "GT-OUT-OF-ORDER-TRANSITION",
        "title": "Out-of-order workflow transition probe",
        "categories": ["workflow", "business_logic"],
        "technique": ("Create a fresh resource in its initial state and invoke a later action "
                      "directly, skipping the documented prerequisite."),
        "expected": "The action is refused until the prerequisite step has happened.",
        "evidence_hint": "Record the starting state, the skipped step and the observed status.",
        "limitations": "The probe needs a known action route; a static route map alone may not "
                       "describe the state machine.",
    },
    {
        "id": "GT-INDUCED-FAILURE-ATOMICITY",
        "title": "Induced-failure persistence probe",
        "categories": ["atomicity", "database"],
        "technique": ("Cause an operation to fail after its writes begin, then compare every "
                      "affected table count before and after."),
        "expected": "No row remains when the operation returns an error.",
        "evidence_hint": "Record before/after counts per table and the failing status.",
        "limitations": "Requires a read-only database connection and a deterministic way to force "
                       "the failure.",
    },
    {
        "id": "GT-UTC-BOUNDARY",
        "title": "UTC day-boundary probe",
        "categories": ["timezone", "api"],
        "technique": ("Submit or query timestamps around midnight UTC and compare the reported day "
                      "with the stored value."),
        "expected": "Day grouping is stable and uses the documented timezone.",
        "evidence_hint": "Record the input timestamp, the returned timestamp and the observed day.",
        "limitations": "Without control of the clock only near-midnight runs exercise the boundary.",
    },
    {
        "id": "GT-BOUNDED-CONCURRENCY",
        "title": "Bounded concurrent submission probe",
        "categories": ["concurrency", "reliability"],
        "technique": ("Issue a small, fixed number of simultaneous equivalent requests and count "
                      "the records created."),
        "expected": "The invariant holds under the documented concurrency limit.",
        "evidence_hint": "Record the concurrency level, the statuses and the final count.",
        "limitations": "A bounded probe can miss a race that needs a different load shape; it is "
                       "not a stress test.",
    },
    {
        "id": "GT-VIEWPORT-OVERFLOW",
        "title": "Mobile viewport overflow check",
        "categories": ["responsive", "ux"],
        "technique": ("Render each primary page at a narrow mobile viewport and compare document "
                      "scroll width with the viewport width."),
        "expected": "The page has no horizontal overflow and primary actions stay reachable.",
        "evidence_hint": "Record the viewport, the page route and a screenshot with the measured "
                         "widths.",
        "limitations": "Overflow alone is not a usability verdict; review the screenshot.",
    },
    {
        "id": "GT-ACCESSIBILITY-BASELINE",
        "title": "Accessibility baseline scan",
        "categories": ["accessibility", "ux"],
        "technique": ("Run an automated accessibility rule set on primary pages and capture "
                      "keyboard reachability for primary actions."),
        "expected": "No serious automated violations are introduced by the change.",
        "evidence_hint": "Record rule ids and selectors, never the page's private content.",
        "limitations": "Automated checks cover a fraction of accessibility requirements; manual "
                       "review remains necessary.",
    },
    {
        "id": "GT-IMPACT-SELECTED-REGRESSION",
        "title": "Impact-selected regression replay",
        "categories": ["regression", "reliability"],
        "technique": ("Walk the dependency graph from each changed file and replay only stored "
                      "regression tests whose findings reference a reached path."),
        "expected": "Tests linked to the change run first; unrelated tests are excluded with a "
                    "reason.",
        "evidence_hint": "Record the changed paths, the selected test ids and the exclusion reasons.",
        "limitations": "Static reachability is an approximation; an unlinked test can still be "
                       "affected by the change.",
    },
)


def build() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "scope": "global",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "techniques": [dict(item) for item in TECHNIQUES],
        "notes": [
            "This library is curated technique guidance. It contains no project identifier, "
            "repository path, rule text, finding title or target address.",
            "A technique describes how to probe a class of defect; it is not evidence that any "
            "application has that defect.",
            "Techniques are candidates for a plan, never an authorization to execute one.",
        ],
    }


def save() -> dict[str, Any]:
    document = build()
    folder = paths.global_knowledge_dir()
    json_path = evidence.write_json(folder, "techniques.json", document, kind="global-knowledge")
    md_path = evidence.write_bytes(folder, "techniques.md", _markdown(document).encode("utf-8"),
                                   kind="global-knowledge")
    items = []
    for technique in document["techniques"]:
        items.append(store.upsert_knowledge_item({
            "item_id": technique["id"],
            "scope": "global",
            "kind": "technique",
            "title": technique["title"],
            "body_path": evidence.relative(json_path),
            "tags": technique["categories"],
        }))
    return {"json": evidence.relative(json_path), "markdown": evidence.relative(md_path),
            "techniques": len(items), "items": items}


def _markdown(document: dict[str, Any]) -> str:
    lines = ["# KHUNKHAAR global technique library", "",
             "Technique-level guidance only. No project data is stored in this file.", ""]
    for technique in document["techniques"]:
        lines += [
            f"## {technique['title']}",
            "",
            f"- id: `{technique['id']}`",
            f"- categories: {', '.join(technique['categories'])}",
            f"- technique: {technique['technique']}",
            f"- expected: {technique['expected']}",
            f"- evidence: {technique['evidence_hint']}",
            f"- limitations: {technique['limitations']}",
            "",
        ]
    lines += ["## Notes", ""] + [f"- {note}" for note in document["notes"]]
    return "\n".join(lines) + "\n"
