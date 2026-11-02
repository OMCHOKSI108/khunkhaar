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

    # Node.js / JavaScript / TypeScript manifests
    pkg_jsons = [p for p in [root / "package.json"] + list(root.glob("*/package.json")) if p.is_file() and not any(part in SKIP_SCAN_DIRS for part in p.parts)]
    for pkg_json in pkg_jsons[:5]:
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


def extract_graphql_endpoints(root: Path) -> list[EndpointDef]:
    """Extract queries and mutations from .graphql and .gql schema files."""
    endpoints: list[EndpointDef] = []
    block_re = re.compile(r'(?:extend\s+)?type\s+(Query|Mutation)\b[^{]*\{([^}]+)\}', re.MULTILINE | re.DOTALL)
    field_re = re.compile(r'^\s*(\w+)(?:\(([^)]*)\))?\s*:\s*([^\s@#]+)(.*)$', re.MULTILINE)

    for path in root.rglob("*.graphql*"):
        if any(part in SKIP_SCAN_DIRS for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for block_match in block_re.finditer(content):
            kind = block_match.group(1)  # Query or Mutation
            body = block_match.group(2)
            for fm in field_re.finditer(body):
                field_name = fm.group(1)
                args_raw = fm.group(2) or ""
                return_type = fm.group(3).strip()
                extra = fm.group(4) or ""
                
                # Check auth / permissions
                auth_req = ("@isAuthenticated" in extra) or ("@hasRole" in extra) or ("@hasPermission" in extra)
                roles = re.findall(r'role[s]?\s*:\s*\[?([A-Za-z0-9_,\s]+)\]?', extra)
                parsed_roles = [r.strip().strip('"\'') for item in roles for r in item.split(",") if r.strip()]

                endpoints.append(EndpointDef(
                    name=field_name,
                    kind=f"graphql_{kind.lower()}",
                    path=f"/graphql#{field_name}",
                    method="POST",
                    source_file=str(path.relative_to(root)),
                    auth_required=auth_req,
                    description=f"{kind} returning {return_type}. Directives: {extra.strip()}"
                ))
    return endpoints


def extract_rest_and_ui_routes(root: Path) -> list[EndpointDef]:
    """Extract REST API endpoints, Next.js routes, and Streamlit UI pages."""
    endpoints: list[EndpointDef] = []
    # Streamlit pages
    for page_path in root.glob("pages/*.py"):
        endpoints.append(EndpointDef(
            name=page_path.stem,
            kind="page",
            path=f"/pages/{page_path.stem}",
            method="GET",
            source_file=str(page_path.relative_to(root)),
            description=f"Streamlit page: {page_path.stem}"
        ))

    # Next.js App router routes
    for route_file in root.glob("**/app/**/route.[jt]s"):
        if any(part in SKIP_SCAN_DIRS for part in route_file.parts):
            continue
        rel = route_file.relative_to(root)
        url_path = "/" + "/".join(p for p in rel.parts if p not in ("app", "route.ts", "route.js"))
        try:
            content = route_file.read_text(encoding="utf-8", errors="ignore")
            for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                if f"export async function {m}" in content or f"export function {m}" in content:
                    endpoints.append(EndpointDef(
                        name=f"{m} {url_path}",
                        kind="rest",
                        path=url_path,
                        method=m,
                        source_file=str(rel),
                        description=f"Next.js App Route handler {m}"
                    ))
        except Exception:
            pass

    return endpoints


def extract_database_models(root: Path) -> list[ModelDef]:
    """Extract database models from Sequelize, Prisma, Mongoose, and Python ORMs."""
    models: list[ModelDef] = []

    # Prisma
    for prisma_file in root.glob("**/schema.prisma"):
        try:
            content = prisma_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'model\s+(\w+)\s*\{([^}]+)\}', content):
                name = m.group(1)
                body = m.group(2)
                fields = [line.strip().split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("//") and not line.strip().startswith("@@")]
                models.append(ModelDef(name=name, table_name=name.lower(), fields=fields[:20], source_file=str(prisma_file.relative_to(root))))
        except Exception:
            pass

    # Sequelize & JavaScript/TypeScript models
    for mfile in root.rglob("*models*/**/*.[jt]s"):
        if any(part in SKIP_SCAN_DIRS for part in mfile.parts):
            continue
        try:
            content = mfile.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r'sequelize\.define\(\s*[\'"](\w+)[\'"]', content)
            if match:
                models.append(ModelDef(name=match.group(1), table_name=match.group(1).lower(), source_file=str(mfile.relative_to(root))))
            else:
                class_match = re.search(r'class\s+(\w+)\s+extends\s+(?:Model|Sequelize\.Model)', content)
                if class_match:
                    models.append(ModelDef(name=class_match.group(1), table_name=class_match.group(1).lower(), source_file=str(mfile.relative_to(root))))
        except Exception:
            pass

    return models


def extract_documentation_and_business_rules(root: Path) -> dict[str, str]:
    """Scan docs, READMEs, and architecture files for business rules and domain logic."""
    doc_snippets: dict[str, str] = {}
    doc_candidates = [root / "README.md", root / "AGENTS.md", root / "CONTRIBUTING.md", root / "ARCHITECTURE.md"]
    
    # Also look into Docs/ and docs/
    for doc_dir in [root / "Docs", root / "docs", root / "doc"]:
        if doc_dir.is_dir():
            doc_candidates.extend(list(doc_dir.glob("*.md"))[:10])

    for doc_file in doc_candidates:
        if doc_file.is_file():
            try:
                rel = str(doc_file.relative_to(root))
                text = doc_file.read_text(encoding="utf-8", errors="ignore").strip()
                # Store concise summary / head of each doc (first 1000 chars)
                if text:
                    doc_snippets[rel] = text[:1500]
            except Exception:
                pass
    return doc_snippets


def extract_environment_variables(root: Path) -> list[str]:
    """Safely extract environment variable names without leaking secrets."""
    keys: set[str] = set()
    env_files = [root / ".env.example", root / ".env.sample", root / ".env.template", root / ".env"]
    for env_file in env_files:
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k = line.split("=", 1)[0].strip()
                        if k and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k):
                            keys.add(k)
            except Exception:
                pass
    return sorted(keys)


def scan_project(root: Path | str = ".") -> ProjectRecon:
    """Perform comprehensive, universal reconnaissance of a target repository."""
    root_path = Path(root).resolve()
    lang, frameworks, proj_type = detect_frameworks_and_language(root_path)
    lang, frameworks, proj_type = detect_python_and_other_frameworks(root_path, lang, frameworks, proj_type)

    # Active services on local ports
    active_ports = discover_active_services()

    # Endpoints
    endpoints = extract_graphql_endpoints(root_path)
    endpoints.extend(extract_rest_and_ui_routes(root_path))

    # Database models
    models = extract_database_models(root_path)

    # Business documentation
    docs = extract_documentation_and_business_rules(root_path)

    # Environment variables
    env_vars = extract_environment_variables(root_path)

    # Auth mechanisms
    auth_mechanisms: list[str] = []
    if any(ep.auth_required for ep in endpoints):
        auth_mechanisms.append("RBAC / Token-based")
    if any("jwt" in k.lower() for k in env_vars):
        auth_mechanisms.append("JWT")
    if any("oauth" in k.lower() for k in env_vars):
        auth_mechanisms.append("OAuth")

    # Determine suggested target URL from active ports
    suggested_url = ""
    for p in active_ports:
        if p.service_type in ("http", "graphql", "streamlit", "nextjs"):
            suggested_url = f"http://localhost:{p.port}"
            break

    proj_id = hashlib.sha256(str(root_path).encode("utf-8")).hexdigest()[:12]
    return ProjectRecon(
        project_id=proj_id,
        project_name=root_path.name,
        root_path=str(root_path),
        primary_language=lang,
        frameworks=sorted(set(frameworks)),
        project_type=proj_type,
        endpoints=endpoints,
        models=models,
        ports=active_ports,
        auth_mechanisms=auth_mechanisms,
        documentation_sources=list(docs.keys()),
        suggested_target_url=suggested_url,
    )

