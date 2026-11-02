"""Discovery Engine: scans project files, languages, frameworks, and entry points."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.context_builder.models import FileInfo

DEFAULT_EXCLUDED_DIRS = {
    "node_modules", ".git", "dist", "build", "coverage", ".next", "vendor",
    "__pycache__", ".venv", "venv", ".idea", ".vscode", "tmp", "logs"
}
DEFAULT_EXCLUDED_EXTS = {
    ".lock", ".min.js", ".map", ".pem", ".key", ".crt", ".log", ".png",
    ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".sqlite3"
}
ENTRYPOINT_NAMES = {
    "main.ts", "server.ts", "app.ts", "index.ts", "main.js", "server.js",
    "app.js", "index.js", "main.py", "app.py", "server.py", "wsgi.py"
}
LANG_EXTENSIONS = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".py": "Python", ".go": "Go", ".java": "Java", ".rs": "Rust", ".sql": "SQL",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".html": "HTML", ".css": "CSS",
}


def compute_file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()[:65536]).hexdigest()[:16]
    except Exception:
        return ""


def scan_directory(root_path: Path, max_files: int = 2000) -> list[FileInfo]:
    files: list[FileInfo] = []
    if not root_path.exists():
        return files
    for root, dirs, filenames in os.walk(root_path):
        dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDED_DIRS and not d.startswith(".")]
        for name in filenames:
            if name == ".env" or any(name.endswith(ext) for ext in DEFAULT_EXCLUDED_EXTS):
                continue
            fpath = Path(root) / name
            try:
                rel = str(fpath.relative_to(root_path))
                ext = fpath.suffix.lower()
                lang = LANG_EXTENSIONS.get(ext, "Unknown")
                size = fpath.stat().st_size
                is_entry = name in ENTRYPOINT_NAMES
                fhash = compute_file_hash(fpath)
                files.append(FileInfo(str(fpath), rel, size, lang, is_entry, fhash))
                if len(files) >= max_files:
                    return files
            except Exception:
                continue
    return files


def detect_frameworks_and_deps(root_path: Path) -> dict[str, Any]:
    deps: list[str] = []
    frameworks: list[str] = []
    pkg_json = root_path / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            deps = list(all_deps.keys())
            if "express" in all_deps: frameworks.append("Express")
            if "nestjs" in all_deps or "@nestjs/core" in all_deps: frameworks.append("NestJS")
            if "graphql" in all_deps or "apollo-server" in all_deps: frameworks.append("GraphQL")
            if "sequelize" in all_deps: frameworks.append("Sequelize")
            if "@prisma/client" in all_deps or "prisma" in all_deps: frameworks.append("Prisma")
            if "typeorm" in all_deps: frameworks.append("TypeORM")
            if "react" in all_deps: frameworks.append("React")
            if "next" in all_deps: frameworks.append("Next.js")
        except Exception:
            pass
    reqs = root_path / "requirements.txt"
    if reqs.exists():
        try:
            content = reqs.read_text(encoding="utf-8").lower()
            deps.extend([l.split("==")[0].strip() for l in content.splitlines() if l.strip()])
            if "fastapi" in content: frameworks.append("FastAPI")
            if "flask" in content: frameworks.append("Flask")
            if "django" in content: frameworks.append("Django")
            if "sqlalchemy" in content: frameworks.append("SQLAlchemy")
        except Exception:
            pass
    return {"frameworks": sorted(set(frameworks)), "dependencies": deps[:100]}
