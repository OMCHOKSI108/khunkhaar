"""Business-rule extraction and classification.

Rules are taken from documentation markers (`RULE R1: ...`, or bullets under a "Business
rules" heading) and from code comments that carry the same marker. Each rule keeps its file and
line, so a finding can point at the exact requirement it violates. Rules without an explicit
marker are not invented; they are recorded at lower confidence only when a rules document uses
normative language.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.intelligence.mapper import ApplicationMap, Rule

RULE_MARK = re.compile(r"(?im)^\s*(?:#|//|/\*|\*)?\s*RULE\s+(?P<id>R?\d{1,3})\s*[:\-–]\s*(?P<text>.+?)\s*(?:\*/)?$")
NUMBERED = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?P<id>R\d{1,3})\s*[:\-–]\s*(?P<text>.+?)\s*$")
NORMATIVE = re.compile(r"(?im)^\s*[-*]\s*(?P<text>(?:must|shall|may only|never|cannot|requires?)\b.+?)\s*$")
HEADING = re.compile(r"(?im)^\s*(?:#{1,6}\s*)?(?:business rules?|requirements?|rules?)\s*:?\s*$")

CLASSIFIERS: list[tuple[str, re.Pattern]] = [
    ("authorization", re.compile(
        r"\b(own|owner|another user|other user|only read|only access|permission|admin|"
        r"unauthorized|forbidden|role|idor|bola|access control)\b", re.I)),
    ("atomicity", re.compile(
        r"\b(partial|partially|transaction|rollback|atomic|failed operation|leave .*data|"
        r"leave .*behind|consistent)\b", re.I)),
    ("idempotency", re.compile(
        r"\b(duplicate|duplicates|twice|repeat|idempotent|double[- ]?submi|same .*twice|"
        r"more than once)\b", re.I)),
    ("concurrency", re.compile(
        r"\b(concurrent|race condition|parallel|simultaneous|at the same time)\b|"
        r"\b(?:only\s+)?one\b[^.;]{0,40}\bper\s+user\b", re.I)),
    ("timezone", re.compile(
        r"\b(utc|timezone|time zone|day boundar|midnight|local time|dst|daylight)\b", re.I)),
    ("responsive", re.compile(
        r"\b(responsive|mobile|390|viewport|horizontal\s+scroll\w*|scroll\s+horizontally|"
        r"screen\s+width)", re.I)),
    ("workflow", re.compile(
        r"\b(approve|approval|submitted?|state transition|only after|before|workflow|orders?|"
        r"skip|draft|clock[- ]?(in|out)|step)\b", re.I)),
    ("validation", re.compile(
        r"\b(empty|null|positive|negative|required|maximum|minimum|length|validat|non-empty|"
        r"amount|must be)\b", re.I)),
    ("ux", re.compile(
        r"\b(error message|loading|empty state|feedback|accessible|keyboard|contrast|label|"
        r"usability|focus)\b", re.I)),
]


def classify(text: str) -> str:
    for kind, pattern in CLASSIFIERS:
        if pattern.search(text):
            return kind
    return "other"


def _rule_id(raw: str | None, index: int) -> str:
    if raw:
        return raw.upper().lstrip("R") if raw.upper().startswith("R") else raw.upper()
    return f"AUTO{index}"


def extract_rules(source: str, text: str) -> list[Rule]:
    """Extract explicit rules from one documentation or source file."""
    is_doc = Path(source).suffix.lower() in (".md", ".rst", ".txt")
    found: list[Rule] = []
    seen: set[tuple[str, str]] = set()

    for match in RULE_MARK.finditer(text):
        rid = _rule_id(match.group("id"), len(found) + 1)
        body = match.group("text").strip()
        if (rid, body) in seen:
            continue
        seen.add((rid, body))
        found.append(Rule(rid, body, source, text[:match.start()].count("\n") + 1,
                          classify(body), 0.9, "explicit"))

    if not is_doc:
        return found

    for match in NUMBERED.finditer(text):
        rid = match.group("id").upper()
        body = match.group("text").strip()
        if (rid, body) in seen:
            continue
        # A numbered list is only a rule when the document gives it normative language.
        if not re.search(r"\b(must|shall|may only|never|cannot|requires?|only|must not)\b", body, re.I):
            continue
        seen.add((rid, body))
        found.append(Rule(rid, body, source, text[:match.start()].count("\n") + 1,
                          classify(body), 0.85, "explicit"))

    if HEADING.search(text):
        for match in NORMATIVE.finditer(text):
            body = match.group("text").strip()
            if any(body == rule.text for rule in found):
                continue
            rid = f"DOC{len(found) + 1}"
            if any(rule.rule_id == rid for rule in found):
                continue
            found.append(Rule(rid, body, source, text[:match.start()].count("\n") + 1,
                              classify(body), 0.6, "inferred"))
    return found


def invariants_from_rules(app_map: ApplicationMap) -> list[dict]:
    """Turn classified rules into invariants with a verification method and confidence."""
    method_by_kind = {
        "authorization": "cross-user resource access probe",
        "validation": "invalid/empty/extreme input probe",
        "workflow": "out-of-order transition probe",
        "idempotency": "repeated identical submission probe",
        "timezone": "UTC boundary data probe",
        "atomicity": "induced failure + persistence probe",
        "responsive": "mobile viewport overflow check",
        "concurrency": "bounded concurrent request probe",
        "ux": "browser UX/heuristic inspection",
        "other": "manual review required",
    }
    features = app_map.features
    out: list[dict] = []
    for rule in app_map.rules:
        feature = "application"
        lowered = rule.text.lower()
        for candidate in features:
            if candidate.replace("_", " ") in lowered or candidate in lowered:
                feature = candidate
                break
        if feature == "application":
            for keyword in ("request", "clock", "report", "user", "order", "invoice", "login"):
                if keyword in lowered:
                    feature = keyword
                    break
        out.append({
            "id": f"INV-{rule.rule_id}",
            "rule_id": rule.rule_id,
            "description": rule.text,
            "source": f"{rule.source}:{rule.line}",
            "feature": feature,
            "roles": [],
            "method": method_by_kind.get(rule.kind, method_by_kind["other"]),
            "kind": rule.kind,
            "confidence": rule.confidence,
            "entities": [e["name"] for e in app_map.entities][:12],
            "origin": rule.origin,
        })
    return out
