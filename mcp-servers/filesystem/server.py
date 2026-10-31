"""KHUNKHAAR Filesystem & Evidence MCP server.

Gives agents a safe working surface: read and write only inside the declared roots
(evidence, reports, logs, runtime/tmp, data, projects/registry, intelligence), never outside
the KHUNKHAAR tree, and never a file whose name is on the deny list (.env, service.json, keys).

Path containment is checked on the fully resolved real path, so symlinks and `..` cannot escape.

Run: ~/KHUNKHAAR-QA/.venv/bin/python ~/KHUNKHAAR-QA/mcp-servers/filesystem/server.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.shared import evidence as ev  # noqa: E402
from core.shared import paths  # noqa: E402
from core.shared.config import current  # noqa: E402
from core.shared.redaction import redact_text  # noqa: E402
from core.shared.toollog import get_logger  # noqa: E402

logger = get_logger("mcp_fs", "mcp_fs.log")

server = MCPServer(
    name="khunkhaar-filesystem",
    version="0.1.0",
    instructions=(
        "Read/write KHUNKHAAR QA artifacts only. Paths are relative to the KHUNKHAAR root and must "
        "land inside one of: evidence, reports, logs, runtime/tmp, data, projects/registry, "
        "intelligence. Source code, .env files and credential stores are out of scope by design - "
        "use the project's own tools for those."
    ),
)

WRITABLE = {"evidence", "reports", "logs", "runtime/tmp", "data", "projects/registry", "intelligence"}


def _contain(raw: str, must_write: bool = False) -> tuple[Path | None, str]:
    if not raw or "\x00" in raw:
        return None, "empty or invalid path"
    root = paths.kh_root().resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, f"cannot resolve path: {exc}"
    if root not in resolved.parents and resolved != root:
        return None, f"path escapes the KHUNKHAAR root"
    rel = str(resolved.relative_to(root)) if resolved != root else "."
    deny = {str(d).lower() for d in current().get("filesystem", "deny_names", []) or []}
    if resolved.name.lower() in deny or any(part.lower() in deny for part in resolved.parts):
        return None, f"'{resolved.name}' is on the filesystem deny list and cannot be accessed"
    # Suffix rules catch *operator-named* credential files (.env, *.env, *.pem, *.key) that an
    # exact-name deny list would miss. data/profiles holds the credential store itself.
    if resolved.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"} or resolved.name.lower() == ".env":
        return None, f"'{resolved.name}' looks like a credential file and cannot be accessed"
    if rel.startswith("data/profiles"):
        return None, "data/profiles is the credential store and is never exposed through this tool"
    roots = current().get("filesystem", "roots", []) or []
    top = rel.split("/")[0] if rel != "." else "."
    if top != "." and not any(rel == r or rel.startswith(r + "/") or r == top for r in roots):
        return None, f"'{top}/' is not a declared KHUNKHAAR root (allowed: {', '.join(roots)})"
    if must_write and top not in WRITABLE and not any(rel.startswith(w) for w in WRITABLE):
        return None, f"'{top}/' is not writable through this tool"
    return resolved, rel


def _err(msg: str, kind: str = "refused") -> dict[str, Any]:
    logger.info("FS %s: %s", kind, redact_text(msg)[:250])
    return {"ok": False, "error_kind": kind, "error": redact_text(msg)[:500]}


@server.tool(description="Resolve a path and report whether it is inside an allowed KHUNKHAAR root and not on the deny list - without touching the file.")
def fs_resolve(path: str, for_write: bool = False) -> dict[str, Any]:
    resolved, decision = _contain(path, must_write=for_write)
    if resolved is None:
        return {"ok": False, "allowed": False, "path": path, "reason": decision}
    return {"ok": True, "allowed": True, "input": path, "relative": decision,
            "exists": resolved.exists(), "is_dir": resolved.is_dir(),
            "size_bytes": resolved.stat().st_size if resolved.is_file() else None}


@server.tool(description="Read a text file inside the KHUNKHAAR tree. Returns content truncated to the configured byte cap.")
def fs_read(path: str, max_bytes: int | None = None) -> dict[str, Any]:
    resolved, rel = _contain(path)
    if resolved is None:
        return _err(rel, "path_refused")
    if not resolved.is_file():
        return _err(f"not a file: {rel}", "not_found")
    cap = min(int(max_bytes or current().get("filesystem", "max_read_bytes", 1_000_000)),
              int(current().get("filesystem", "max_read_bytes", 1_000_000)))
    size = resolved.stat().st_size
    data = resolved.read_bytes()[:cap]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": True, "path": rel, "binary": True, "size_bytes": size,
                "note": "binary file; use evidence_save/fs_write_text for text artifacts"}
    # Content still crosses into model context: strip recognisable credential formats first.
    return {"ok": True, "path": rel, "size_bytes": size, "truncated": size > cap,
            "content": redact_text(text)}


@server.tool(description="Write or overwrite a UTF-8 text artifact inside a writable KHUNKHAAR root (evidence, reports, logs, runtime/tmp, data, projects, intelligence).")
def fs_write_text(path: str, content: str, mkdir: bool = True) -> dict[str, Any]:
    resolved, rel = _contain(path, must_write=True)
    if resolved is None:
        return _err(rel, "path_refused")
    if resolved.is_dir():
        return _err(f"{rel} is a directory", "invalid_target")
    if mkdir:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    logger.info("WROTE %s (%s bytes)", rel, len(content))
    return {"ok": True, "path": rel, "bytes_written": len(content.encode())}


@server.tool(description="Store binary evidence (screenshot, trace, download, payload) from base64 content under evidence/<kind>/.")
def evidence_save(kind: str, name: str, content_base64: str, run_id: str | None = None) -> dict[str, Any]:
    safe_kind = ev.slugify(kind, 40)
    folder = ev.evidence_root(safe_kind, run_id or ev.new_run_id(safe_kind))
    target = folder / ev.slugify(name, 80)
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        return _err(f"content is not valid base64: {exc}", "invalid_input")
    limit = int(current().get("filesystem", "max_read_bytes", 1_000_000)) * 5
    if len(data) > limit:
        return _err(f"payload is {len(data)} bytes, limit is {limit}", "too_large")
    target.write_bytes(data)
    target.chmod(0o600)
    return {"ok": True, "path": ev.relative(target), "bytes": len(data)}


@server.tool(description="List evidence for a kind and optionally one run, returning paths, sizes and modification times.")
def evidence_list(kind: str | None = None, run_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    base = paths.evidence_dir() if not kind else ev.evidence_root(ev.slugify(kind, 40))
    if run_id:
        base = base / ev.slugify(run_id, 60)
    if not base.is_dir():
        return {"ok": True, "base": str(base.relative_to(paths.kh_root())), "count": 0, "entries": []}
    entries = []
    for item in sorted(base.rglob("*")):
        if item.is_file():
            stat = item.stat()
            entries.append({"path": ev.relative(item), "bytes": stat.st_size,
                            "modified": int(stat.st_mtime)})
        if len(entries) >= limit:
            break
    return {"ok": True, "base": str(base.relative_to(paths.kh_root())), "count": len(entries),
            "truncated": len(entries) >= limit, "entries": entries}


@server.tool(description="List the contents of an allowed directory inside the KHUNKHAAR tree.")
def fs_list(path: str = ".", limit: int = 200) -> dict[str, Any]:
    resolved, rel = _contain(path)
    if resolved is None:
        return _err(rel, "path_refused")
    if not resolved.is_dir():
        return _err(f"{rel} is not a directory", "invalid_target")
    items = []
    for child in sorted(resolved.iterdir()):
        items.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                      "bytes": child.stat().st_size if child.is_file() else None})
        if len(items) >= limit:
            break
    return {"ok": True, "path": rel, "count": len(items), "entries": items}


@server.tool(description="Create a directory inside a writable KHUNKHAAR root.")
def fs_mkdir(path: str) -> dict[str, Any]:
    resolved, rel = _contain(path, must_write=True)
    if resolved is None:
        return _err(rel, "path_refused")
    resolved.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": rel, "created": resolved.is_dir()}


@server.tool(description="Delete a single file, restricted to runtime/tmp. Evidence and reports are protected by design - use fs_tmp_cleanup for scheduled pruning.")
def fs_delete(path: str) -> dict[str, Any]:
    resolved, rel = _contain(path, must_write=True)
    if resolved is None:
        return _err(rel, "path_refused")
    if not rel.startswith("runtime/tmp/"):
        return _err(
            "deletion is only permitted inside runtime/tmp; evidence, reports and project data are "
            "retained by policy", "protected_path")
    if not resolved.is_file():
        return _err(f"{rel} is not a file", "invalid_target")
    resolved.unlink()
    return {"ok": True, "removed": rel}


@server.tool(description="Apply the documented cleanup policy: prune files in runtime/tmp older than the configured TTL. Never touches evidence or reports.")
def fs_tmp_cleanup(max_age_hours: int | None = None) -> dict[str, Any]:
    hours = max_age_hours or int(current().get("filesystem", "tmp_ttl_hours", 24))
    removed = ev.cleanup_tmp(hours)
    logger.info("tmp cleanup removed %s files older than %sh", len(removed), hours)
    return {"ok": True, "ttl_hours": hours, "removed_count": len(removed), "removed": removed[:200]}


@server.tool(description="Summarise disk usage of the KHUNKHAAR artifact areas so a run can be planned against free space.")
def fs_storage_report() -> dict[str, Any]:
    root = paths.kh_root()
    areas = ["evidence", "reports", "logs", "runtime/tmp", "data", "projects/registry", "intelligence"]
    out = []
    for area in areas:
        target = root / area
        if not target.exists():
            out.append({"area": area, "exists": False, "bytes": 0, "files": 0})
            continue
        total = 0
        count = 0
        for f in target.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
                count += 1
        out.append({"area": area, "exists": True, "bytes": total, "files": count})
    import shutil

    usage = shutil.disk_usage(root)
    return {"ok": True, "areas": out, "total_bytes": sum(a["bytes"] for a in out),
            "disk_free_bytes": usage.free, "disk_total_bytes": usage.total}


if __name__ == "__main__":
    logger.info("starting khunkhaar-fs MCP server over stdio")
    server.run(transport="stdio")
