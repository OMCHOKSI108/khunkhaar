"""WCAG Profiles and Standards configuration for axe-core and browser inspections."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from core.shared import paths

WCAG_TAG_PROFILES: dict[str, list[str]] = {
    "wcag_20_aa": ["wcag2a", "wcag2aa"],
    "wcag_21_aa": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
    "wcag_22_aa": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"],
    "best_practice": ["best-practice"],
    "comprehensive": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"],
}

AXE_PATH = paths.kh_root() / "opt" / "axe" / "axe.min.js"


def get_wcag_tags(profile_name: str = "wcag_22_aa") -> list[str]:
    profile_key = (profile_name or "wcag_22_aa").lower()
    return WCAG_TAG_PROFILES.get(profile_key, WCAG_TAG_PROFILES["wcag_22_aa"])


def get_axe_asset_metadata() -> dict[str, Any]:
    if not AXE_PATH.is_file():
        return {"installed": False, "version": "unknown", "hash": None}

    content = AXE_PATH.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()

    # Read package.json if present
    pkg_file = AXE_PATH.parent / "package.json"
    version = "4.x"
    if pkg_file.is_file():
        try:
            import json
            data = json.loads(pkg_file.read_text(encoding="utf-8"))
            version = data.get("version", version)
        except Exception:
            pass

    return {
        "installed": True,
        "version": version,
        "sha256": sha256,
        "file_size": len(content),
    }


def map_axe_impact_to_severity(impact: str | None) -> str:
    mapping = {
        "critical": "critical",
        "serious": "high",
        "moderate": "medium",
        "minor": "low",
    }
    return mapping.get((impact or "").lower(), "medium")
