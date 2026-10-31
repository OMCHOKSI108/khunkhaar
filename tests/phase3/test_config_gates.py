"""The Phase 3 safety gates that configuration is not allowed to open."""

from __future__ import annotations

from core.shared import config as config_module


def _write(root, text: str):
    path = root / "config" / "khunkhaar.yaml"
    path.write_text(text, encoding="utf-8")
    config_module._CURRENT = None
    return config_module.load_config(path)


def test_git_allowlist_can_narrow_but_never_widen(kh_root):
    config = _write(kh_root, "git:\n  read_only_commands: [log, diff]\n")
    assert config.git_commands() == frozenset({"log", "diff"})

    hostile = _write(kh_root, "git:\n  read_only_commands: [log, push, commit, checkout, "
                              "clean, reset, gc, config]\n")
    assert hostile.git_commands() == frozenset({"config", "log"})
    assert not hostile.git_allowed("push")
    assert not hostile.git_allowed("checkout")
    assert not hostile.git_allowed("clean")
    assert hostile.git_allowed("log")


def test_dashboard_refuses_a_public_bind(kh_root):
    config = _write(kh_root, "dashboard:\n  host: 0.0.0.0\n  port: 8899\n")
    assert config.dashboard_host() == "127.0.0.1"
    assert config.dashboard_port() == 8899
    wildcard = _write(kh_root, "dashboard:\n  host: '::'\n")
    assert wildcard.dashboard_host() == "127.0.0.1"


def test_dashboard_port_is_privileged_free(kh_root):
    config = _write(kh_root, "dashboard:\n  port: 22\n")
    assert config.dashboard_port() >= 1024


def test_retention_defaults_to_no_deletion(kh_root):
    config = _write(kh_root, "retention:\n  evidence_days: 0\n")
    assert int(config.get("retention", "evidence_days")) == 0
    assert config.retention_protect_statuses() == (
        "confirmed", "likely", "unverified", "needs_human", "false_positive")


def test_shipped_configuration_keeps_the_gates_closed():
    """config/khunkhaar.yaml as committed must not authorise anything on its own."""
    config = config_module.load_config()
    assert config.git_commands() <= config_module._GIT_READONLY_FLOOR
    assert config.dashboard_host() == "127.0.0.1"
    assert config.get("monitoring", "enabled") is False
    assert config.get("monitoring", "install_system_scheduler") is False
    assert config.get("dashboard", "enabled") is False
    assert config.get("planner", "require_approval") is True
    assert config.get("regression", "auto_execute") is False
    assert config.get("regression", "require_confirmed_finding") is True
    notifications = config.section("notifications")
    assert notifications["email"]["enabled"] is False
    assert notifications["email"]["recipients"] == []
    assert notifications["webhook"]["enabled"] is False
    assert notifications["webhook"]["url"] is None
    assert config.get("retention", "evidence_days") in (0, None)
    assert config.get("security", "forbid_production_targets") is True
    assert config.get("git", "verify_index_untouched") is True


def test_unknown_section_is_empty(kh_root):
    config = _write(kh_root, "nonsense:\n  key: value\n")
    assert config.section("nonsense") == {"key": "value"}
    assert config.section("does_not_exist") == {}

