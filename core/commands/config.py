"""`khunkhaar config show` — the effective configuration, including clamped values.

What is printed here is what the engine actually enforces, which is not always what the YAML file
literally says: the git command list is intersected with the safe floor and the dashboard bind is
forced to loopback. Any key that looks like a credential is masked by the shared redactor even
though no secret belongs in this file.
"""

from __future__ import annotations

import argparse
import json

from core.shared import paths
from core.shared.config import current
from core.shared.redaction import redact_obj


def _effective() -> dict:
    config = current()
    document = redact_obj(config.raw)
    document["_clamped"] = {
        "git.read_only_commands": sorted(config.git_commands()),
        "dashboard.host": config.dashboard_host(),
        "dashboard.port": config.dashboard_port(),
        "retention.protect_statuses": list(config.retention_protect_statuses()),
        "database.read_only": config.read_only_database,
    }
    document["_root"] = str(paths.kh_root())
    document["_config_file"] = str(paths.config_file("khunkhaar.yaml"))
    return document


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar config show")
    parser.add_argument("--section", help="print one top-level section only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    document = _effective()
    if args.section:
        if args.section not in document:
            print(f"khunkhaar config: no section {args.section!r}. "
                  f"Available: {', '.join(sorted(document))}")
            return 2
        document = {args.section: document[args.section]}
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
        return 0
    for section in sorted(document):
        value = document[section]
        if isinstance(value, dict):
            print(f"[{section}]")
            for key in sorted(value):
                print(f"  {key} = {json.dumps(value[key], default=str)}")
        else:
            print(f"{section} = {json.dumps(value, default=str)}")
    return 0
