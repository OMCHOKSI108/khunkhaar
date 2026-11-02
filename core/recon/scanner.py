"""Universal codebase reconnaissance and architecture extractor."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
import hashlib

from core.recon.models import ProjectRecon, EndpointDef, ModelDef
from core.recon.ports import discover_active_services

SKIP_SCAN_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", "vendor", "coverage",
    ".mypy_cache", ".pytest_cache", ".hypothesis", ".khunkhar", ".khunkhaar"
}


def detect_frameworks_and_language(root: Path) -> tuple[str, list[str], str]:
    """Inspect package manifests to determine primary language, frameworks, and project type."""
    frameworks: list[str] = []
    language = "unknown"
    project_type = "web-app"

    # Node.js
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        language = "JavaScript/TypeScript"
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "graphql" in deps or "apollo-server" in deps or "@apollo/server" in deps:
                frameworks.append("GraphQL")
                project_type = "graphql-api"
            if "express" in deps:
                frameworks.append("Express")
            if "next" in deps:
                frameworks.append("Next.js")
                project_type = "fullstack"
            if "@nestjs/core" in deps:
                frameworks.append("NestJS")
            if "react" in deps and "next" not in deps:
                frameworks.append("React")
                project_type = "frontend"
            if "vue" in deps:
                frameworks.append("Vue")
                project_type = "frontend"
            if "sequelize" in deps:
                frameworks.append("Sequelize ORM")
            if "prisma" in deps or "@prisma/client" in deps:
                frameworks.append("Prisma ORM")
            if "mongoose" in deps:
                frameworks.append("Mongoose ODM")
        except Exception:
            pass
    return language, frameworks, project_type
