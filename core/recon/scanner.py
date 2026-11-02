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


def detect_python_and_other_frameworks(root: Path, language: str, frameworks: list[str], project_type: str) -> tuple[str, list[str], str]:
    """Detect Python, Java, PHP, Go, and Rust frameworks."""
    # Python
    py_markers = [root / "pyproject.toml", root / "requirements.txt", root / "setup.py", root / "Pipfile"]
    for marker in py_markers:
        if marker.is_file():
            language = "Python"
            text = marker.read_text(encoding="utf-8", errors="ignore").lower()
            if "streamlit" in text:
                frameworks.append("Streamlit")
                project_type = "streamlit"
            if "fastapi" in text:
                frameworks.append("FastAPI")
                project_type = "rest-api"
            if "flask" in text:
                frameworks.append("Flask")
            if "django" in text:
                frameworks.append("Django")
            if "strawberry" in text or "graphene" in text:
                frameworks.append("GraphQL")
            if "sqlalchemy" in text:
                frameworks.append("SQLAlchemy")
            break

    # Java
    if (root / "pom.xml").is_file() or (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        language = "Java/Kotlin"
        project_type = "backend"
        frameworks.append("Spring Boot")

    # PHP
    if (root / "composer.json").is_file():
        language = "PHP"
        text = (root / "composer.json").read_text(encoding="utf-8", errors="ignore").lower()
        if "laravel" in text:
            frameworks.append("Laravel")
            project_type = "web-app"

    # Go
    if (root / "go.mod").is_file():
        language = "Go"
        project_type = "service"

    return language, frameworks, project_type
