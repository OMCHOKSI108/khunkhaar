"""Central configuration loading for KHUNKHAAR QA.

Reads config/khunkhaar.yaml and fills in safe defaults. Safety-critical values (timeouts,
allowed methods, host allowlists, row limits) are clamped here rather than trusted from
whatever a caller passes in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import paths

# The set of git invocations KHUNKHAAR is ever allowed to make. Every one of them reads; none
# can move a ref, touch the index, rewrite the working tree or talk to a remote. Configuration
# may only *narrow* this list (see Config.git_commands), so a typo or a hostile edit to
# khunkhaar.yaml can never grant a write-capable command.
_GIT_READONLY_FLOOR = frozenset({
    "rev-parse", "rev-list", "log", "show", "diff", "status", "ls-files", "ls-tree",
    "merge-base", "show-ref", "for-each-ref", "cat-file", "count-objects", "shortlog",
    "config",
})

DEFAULTS: dict[str, Any] = {
    "api": {
        # Empty allowlist means: refuse every target until the operator adds one.
        "allowed_hosts": [],
        "denied_hosts": ["169.254.169.254", "metadata.google.internal"],
        "allow_private_ranges": True,
        "default_timeout_seconds": 10.0,
        "max_timeout_seconds": 120.0,
        "safe_methods": ["GET", "HEAD", "OPTIONS"],
        "mutating_methods": ["POST", "PUT", "PATCH"],
        "destructive_methods": ["DELETE"],
        "require_approval_for_mutating": False,
        "require_approval_for_destructive": True,
        "max_retries": 3,
        "max_request_bytes": 2_000_000,
        "max_response_bytes": 5_000_000,
        "max_body_chars_in_output": 20_000,
        "evidence_enabled": True,
    },
    "database": {
        "default_profile": None,
        "statement_timeout_ms": 5_000,
        "max_rows": 500,
        "max_identifier_len": 128,
        "enforce_read_only": True,
        "profiles": [],
    },
    "browser": {
        "headless": True,
        "default_engine": "chromium",
        "tracing": True,
        "video": False,
        "screenshot_on_action": False,
        "navigation_timeout_ms": 30_000,
        "allowed_origins": [],
        "max_output_bytes": 200_000,
    },
    "filesystem": {
        # All paths are resolved relative to these roots; traversal outside is refused.
        "roots": ["evidence", "reports", "logs", "runtime/tmp", "data", "projects/registry", "intelligence"],
        "max_read_bytes": 1_000_000,
        "deny_names": [".env", "service.json", "id_rsa", "credentials.json", ".git-credentials"],
        "tmp_ttl_hours": 24,
    },
    "security": {
        # Active scanning is gated: no target may be hit unless it is in scope AND authorized.
        "scans_require_authorization": True,
        "authorized_targets": [],
        "forbid_production_targets": True,
    },
    "project_discovery": {
        "max_files_scanned": 40_000,
        "skip_dirs": [
            ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
            ".next", ".nuxt", "target", "vendor", ".mypy_cache", ".pytest_cache",
            "coverage", ".idea", ".gradle", "out",
        ],
        # Policy flag, not a credential.
        "secret_value_ingest": False,  # nosec B105
    },
    "telemetry": {"log_tool_calls": True},
    "git": {
        # The hard floor: config may only narrow this list, never widen it. A subcommand that
        # can write is unreachable no matter what khunkhaar.yaml says.
        "read_only_commands": sorted(_GIT_READONLY_FLOOR),
        "require_registered_project": True,
        "verify_index_untouched": True,
        "max_files_in_report": 2000,
        "max_diff_bytes_per_file": 200_000,
        # Symbol attribution reads both sides of each changed Python file. It is two blob reads per
        # file, so it is capped independently of how many files a report may list.
        "ast_symbol_attribution": True,
        "max_symbol_attribution_files": 60,
        # One extra `git log --name-status -M` per branch/range analysis lists each commit's files.
        "commit_file_lists": True,
        "max_files_per_commit": 40,
        "base_branch_candidates": ["origin/main", "main", "origin/master", "master",
                                   "origin/develop", "develop"],
    },
    "impact": {
        "ast_languages": ["python", "javascript", "typescript", "tsx"],
        "max_source_bytes": 2_000_000,
        "max_files": 4_000,
        "max_symbols_per_file": 500,
        "parse_js_ts": True,
        "max_graph_nodes": 20_000,
        "max_graph_edges": 100_000,
        "max_depth": 4,
        "max_impacted": 500,
        "label_as_estimate": True,
    },
    "regression": {
        "require_confirmed_finding": True,
        "validation_runs": 2,
        "auto_execute": False,
        "quarantine_unreliable": True,
    },
    "gitlab": {
        # Off by default, and even when enabled the host must also be in api.allowed_hosts.
        # Only the environment variable named here is read; its value is never persisted.
        "enabled": False,
        "host": "",
        "project": "",
        "token_env": "KHUNKHAAR_GITLAB_TOKEN",
        "token_header": "PRIVATE-TOKEN",
        "timeout_seconds": 10,
        "max_diff_files": 500,
    },
    "planner": {
        "require_approval": True,
        "always_approve_modes": ["adversarial"],
        "default_mode": "controlled",
        "respect_target_allowlist": True,
        "max_actions": 80,
        # Read-only API probes may run in a bounded pool; browser and journey work stays serial.
        "api_workers": 2,
        "require_approval_for_adversarial": True,
        "require_approval_for_external_target": True,
    },
    "monitoring": {
        "enabled": False,
        "install_system_scheduler": False,
        "max_overdue_before_missed": 2,
        "schedule_grace_seconds": 900,
        "catch_up": False,
        "check_max_seconds": 600,
        "check_timeout_seconds": 120,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8899,
        "enabled": False,
        "require_token": True,  # nosec B105 - policy flag, not a credential
        "bind_non_loopback_requires_flag": True,
    },
    "notifications": {
        "terminal": True,
        "desktop": {"enabled": True, "timeout_seconds": 5},
        "email": {"enabled": False, "smtp_host": None, "recipients": []},
        "webhook": {"enabled": False, "url": None},
    },
    "retention": {
        "evidence_days": 0,
        "tmp_ttl_hours": 24,
        "protect_statuses": ["confirmed", "likely", "unverified", "needs_human", "false_positive"],
        "report_days": 0,
        "max_evidence_bytes": 0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    # ---- clamped accessors: the values that gate unsafe behaviour -------------
    def api_timeout(self, requested: float | None) -> float:
        api = self.section("api")
        cap = float(api["max_timeout_seconds"])
        default = float(api["default_timeout_seconds"])
        value = default if requested in (None, 0) else float(requested)
        return max(0.1, min(value, cap))

    def api_max_retries(self, requested: int | None) -> int:
        cap = int(self.get("api", "max_retries", 3))
        if requested is None:
            return 0
        return max(0, min(int(requested), cap))

    def db_max_rows(self, requested: int | None) -> int:
        cap = int(self.get("database", "max_rows", 500))
        if requested is None:
            return cap
        return max(1, min(int(requested), cap))

    @property
    def read_only_database(self) -> bool:
        return bool(self.get("database", "enforce_read_only", True))

    # ---- git: intersect, never union ----------------------------------------
    def git_commands(self) -> frozenset[str]:
        """Configured read-only git subcommands, clamped to the built-in safe floor."""
        configured = self.get("git", "read_only_commands", []) or []
        names = {str(c).strip() for c in configured}
        return frozenset(names & _GIT_READONLY_FLOOR)

    def git_allowed(self, subcommand: str) -> bool:
        return subcommand in self.git_commands()

    # ---- dashboard: loopback unless the operator forces otherwise ------------
    def dashboard_host(self, override: str | None = None) -> str:
        host = override or str(self.get("dashboard", "host", "127.0.0.1"))
        if host in ("0.0.0.0", "::", "*"):  # nosec B104 - these are refused, never bound
            # Public binds are only reachable through an explicit operator flag, which the
            # caller passes as `override`; the configured default can never expose the board.
            host = "127.0.0.1"
        return host

    def dashboard_port(self, override: int | None = None) -> int:
        port = override if override else int(self.get("dashboard", "port", 8899))
        return max(1024, min(int(port), 65535))

    def retention_protect_statuses(self) -> tuple[str, ...]:
        configured = self.get("retention", "protect_statuses", []) or []
        return tuple(dict.fromkeys(str(s) for s in configured)) or ("confirmed",)


def load_config(path: str | os.PathLike | None = None) -> Config:
    target = path or paths.config_file("khunkhaar.yaml")
    data: dict[str, Any] = {}
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{target} must contain a YAML mapping")
        data = loaded
    return Config(raw=_merge(DEFAULTS, data))


_CURRENT: Config | None = None


def current(force_reload: bool = False) -> Config:
    global _CURRENT
    if _CURRENT is None or force_reload:
        _CURRENT = load_config()
    return _CURRENT
