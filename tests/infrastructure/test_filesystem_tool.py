"""Filesystem & evidence MCP server acceptance tests (Phase 1 section 7).

The tool is the agent's only writable surface inside KHUNKHAAR: it must be useful for artifacts
and useless for exfiltration - no escaping the tree (including through symlinks), no credential
files, no deleting evidence.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from core.shared import paths

pytestmark = pytest.mark.mcp


# ---------------------------------------------------------------- containment


@pytest.mark.parametrize("path", [
    "../outside.txt",
    "evidence/../../../etc/passwd",
    "/etc/passwd",
    "/tmp/escape.txt",
    "config/khunkhaar.yaml",
    ".venv/bin/python",
    "core/shared/config.py",
])
def test_paths_outside_the_declared_roots_are_refused(filesystem_mcp, path):
    result = filesystem_mcp.fs_resolve(path=path)
    assert result["ok"] is False and result["allowed"] is False, (path, result)


@pytest.mark.parametrize("path", [
    "data/profiles/db/khunkhaar_test_pg.env",
    "data/profiles/auth/anything.json",
    ".env",
    "evidence/fake.pem",
    "runtime/tmp/private.key",
])
def test_credential_shaped_paths_are_refused(filesystem_mcp, path):
    assert filesystem_mcp.fs_resolve(path=path)["ok"] is False
    assert filesystem_mcp.fs_read(path=path)["ok"] is False


def test_symlink_cannot_escape_the_tree(filesystem_mcp):
    link = paths.tmp_dir() / "escape-link"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink("/etc/passwd", link)
    except OSError as exc:
        pytest.skip(f"cannot create symlink in this environment: {exc}")
    try:
        assert filesystem_mcp.fs_resolve(path="runtime/tmp/escape-link")["ok"] is False
        assert filesystem_mcp.fs_read(path="runtime/tmp/escape-link")["ok"] is False
    finally:
        link.unlink()


def test_relative_paths_resolve_to_repo_relative_output(filesystem_mcp):
    result = filesystem_mcp.fs_resolve(path="reports")
    assert result["ok"] and result["relative"] == "reports" and result["is_dir"] is True
    assert str(paths.kh_root()) not in result["relative"]


# ---------------------------------------------------------------- read / write


def test_write_read_roundtrip_inside_evidence(filesystem_mcp):
    target = "runtime/tmp/fs-roundtrip.txt"
    content = "hello khunkhaar\n"
    written = filesystem_mcp.fs_write_text(path=target, content=content)
    assert written["ok"] and written["bytes_written"] == len(content.encode())
    read = filesystem_mcp.fs_read(path=target)
    assert read["ok"] is True and read["content"] == "hello khunkhaar\n"
    assert filesystem_mcp.fs_delete(path=target)["ok"] is True


def test_secret_connection_strings_are_redacted_on_read(filesystem_mcp):
    target = "runtime/tmp/dsn-note.txt"
    filesystem_mcp.fs_write_text(path=target, content="dsn=postgresql://user:pa55@db.local:5432/app\n")
    content = filesystem_mcp.fs_read(path=target)["content"]
    assert "pa55" not in content and "REDACTED" in content
    filesystem_mcp.fs_delete(path=target)


def test_writes_outside_writable_roots_are_refused(filesystem_mcp):
    for path in ("config/evil.yaml", "../evil.txt", "core/evil.py"):
        assert filesystem_mcp.fs_write_text(path=path, content="x")["ok"] is False


def test_binary_and_large_reads_are_capped(filesystem_mcp):
    target = "runtime/tmp/large.txt"
    filesystem_mcp.fs_write_text(path=target, content="x" * 5000)
    result = filesystem_mcp.fs_read(path=target, max_bytes=100)
    assert result["ok"] and result["truncated"] is True and len(result["content"]) == 100
    filesystem_mcp.fs_delete(path=target)


# ---------------------------------------------------------------- evidence


def test_evidence_save_creates_a_private_file(filesystem_mcp):
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode()
    saved = filesystem_mcp.evidence_save(kind="mcp-test", name="shot.png", content_base64=payload,
                                         run_id="mcp-run-1")
    assert saved["ok"] and saved["bytes"] == len(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    path = paths.kh_root() / saved["path"]
    assert path.is_file() and path.read_bytes().startswith(b"\x89PNG")
    assert path.stat().st_mode & 0o077 == 0, "evidence must not be world-readable"
    assert saved["path"].startswith("evidence/mcp-test/mcp-run-1/")


def test_evidence_save_rejects_bad_base64_and_oversized_payloads(filesystem_mcp):
    assert filesystem_mcp.evidence_save(kind="mcp-test", name="x.bin",
                                        content_base64="not base64!!")["error_kind"] == "invalid_input"
    big = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
    assert filesystem_mcp.evidence_save(kind="mcp-test", name="big.bin",
                                        content_base64=big)["error_kind"] == "too_large"


def test_evidence_list_finds_saved_artifacts(filesystem_mcp):
    listing = filesystem_mcp.evidence_list(kind="mcp-test", run_id="mcp-run-1")
    assert listing["ok"] and listing["count"] >= 1
    assert any(entry["path"].endswith("shot.png") for entry in listing["entries"])


# ---------------------------------------------------------------- deletion policy


def test_delete_is_restricted_to_runtime_tmp(filesystem_mcp):
    filesystem_mcp.fs_write_text(path="runtime/tmp/disposable.txt", content="bye")
    assert filesystem_mcp.fs_delete(path="runtime/tmp/disposable.txt")["ok"] is True
    # evidence must be protected even though it is writable
    saved = filesystem_mcp.evidence_save(kind="mcp-test", name="keep.png",
                                         content_base64=base64.b64encode(b"keep").decode(),
                                         run_id="mcp-run-1")
    refused = filesystem_mcp.fs_delete(path=saved["path"])
    assert refused["ok"] is False and refused["error_kind"] == "protected_path"
    assert (paths.kh_root() / saved["path"]).is_file()


def test_tmp_cleanup_only_prunes_expired_files(filesystem_mcp):
    old = paths.tmp_dir() / "expired.txt"
    fresh = paths.tmp_dir() / "fresh.txt"
    old.write_text("old")
    fresh.write_text("fresh")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    result = filesystem_mcp.fs_tmp_cleanup(max_age_hours=24)
    assert result["ok"] and not old.exists() and fresh.exists()
    fresh.unlink()


def test_storage_report_covers_every_area(filesystem_mcp):
    report = filesystem_mcp.fs_storage_report()
    assert report["ok"]
    areas = {a["area"] for a in report["areas"]}
    assert {"evidence", "reports", "logs", "runtime/tmp", "data"} <= areas
    assert report["disk_free_bytes"] > 0
