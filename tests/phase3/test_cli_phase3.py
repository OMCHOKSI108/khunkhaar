"""CLI dispatch: Phase 3 subcommands route, Phase 1/2 behaviour is unchanged."""

from __future__ import annotations

import json

import pytest

from core import cli
from core import commands as command_registry
from core.metadata import migrations, store


def test_registry_paths_are_unique():
    names = [spec.name for spec in command_registry.REGISTRY]
    assert len(names) == len(set(names))


def test_every_registered_handler_is_callable():
    """A registry entry that cannot be loaded is a bug, and this proves it is not one."""
    for spec in command_registry.REGISTRY:
        handler = command_registry.handler(spec)
        assert callable(handler.call)


@pytest.mark.parametrize("argv,expected", [
    (["status"], ("status",)),
    (["projects"], ("projects",)),
    (["project", "register", "--path", "."], ("project", "register")),
    (["retention", "plan"], ("retention", "plan")),
    (["config"], ("config", "show")),          # bare group name resolves to its only subcommand
    (["config", "show", "--json"], ("config", "show")),
])
def test_resolve(argv, expected):
    resolved = command_registry.resolve(argv)
    assert resolved is not None
    assert resolved[0].path == expected


def test_resolve_leaves_phase2_commands_to_the_old_dispatcher():
    assert command_registry.resolve(["scan", "--json"]) is None
    assert command_registry.resolve(["report"]) is None


def test_version_and_help_still_work(capsys):
    assert cli.main(["khunkhaar", "--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("khunkhaar ")
    assert cli.main(["khunkhaar", "--help"]) == 0
    assert "PHASE 3 COMMANDS" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error_not_a_crash(capsys):
    assert cli.main(["khunkhaar", "definitely-not-a-command"]) == 2
    assert "unknown command" in capsys.readouterr().out


def test_status_command_reports_the_migrated_schema(kh_root, capsys):
    assert cli.main(["khunkhaar", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"]["current"] == migrations.LATEST_VERSION
    assert payload["schema"]["pending"] == []
    assert payload["projects"] == 0


def test_projects_then_register_then_show(kh_root, tmp_path, capsys):
    repo = tmp_path / "shop"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='shop'\n", encoding="utf-8")

    assert cli.main(["khunkhaar", "projects", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo), "--json"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["authorized_targets"] == []
    assert "authorize" in registered["note"]

    assert cli.main(["khunkhaar", "projects", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["project_id"] for r in rows] == [registered["project_id"]]

    assert cli.main(["khunkhaar", "project", "show", "--project-id",
                     registered["project_id"], "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["project"]["root_path"] == str(repo.resolve())


def test_registering_the_platform_root_is_refused(kh_root, capsys):
    assert cli.main(["khunkhaar", "project", "register", "--path", str(kh_root)]) == 2
    assert "refusing" in capsys.readouterr().out


def test_authorize_requires_registration_and_refuses_production(kh_root, capsys):
    assert cli.main(["khunkhaar", "project", "authorize", "--project-id", "missing-00000000",
                     "--target", "http://127.0.0.1:9", "--mode", "observe"]) == 2
    capsys.readouterr()
    repo = kh_root / "app"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert cli.main(["khunkhaar", "project", "register", "--path", str(repo), "--json"]) == 0
    project_id = json.loads(capsys.readouterr().out)["project_id"]

    assert cli.main(["khunkhaar", "project", "authorize", "--project-id", project_id,
                     "--target", "https://api.production.example", "--mode",
                     "adversarial"]) == 2
    assert "production" in capsys.readouterr().out

    assert cli.main(["khunkhaar", "project", "authorize", "--project-id", project_id,
                     "--target", "http://127.0.0.1:8731", "--mode", "controlled",
                     "--days", "1", "--json"]) == 0
    grant = json.loads(capsys.readouterr().out)
    assert grant["mode"] == "controlled" and grant["expires_at"]
    assert store.is_authorized(project_id, "http://127.0.0.1:8731", "controlled")
    assert not store.is_authorized(project_id, "http://127.0.0.1:8731", "adversarial")
    assert not store.is_authorized("other-00000000", "http://127.0.0.1:8731", "controlled")


def test_retention_apply_without_confirm_deletes_nothing(kh_root, tmp_path, capsys):
    from core.shared import evidence as ev

    store.init()
    artifact = ev.write_json(ev.evidence_root("probe"), "one.json", {"a": 1})
    assert cli.main(["khunkhaar", "retention", "apply", "--days", "0"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert artifact.is_file()


def test_config_show_masks_credential_shaped_keys(kh_root, capsys):
    (kh_root / "config" / "khunkhaar.yaml").write_text(
        "auth:\n  profiles:\n    - {name: staging, api_token: should-not-appear}\n",
        encoding="utf-8")
    assert cli.main(["khunkhaar", "config", "show", "--json"]) == 0
    printed = capsys.readouterr().out
    assert "should-not-appear" not in printed
    assert json.loads(printed)["auth"]["profiles"]
