# OpenCode integration

OpenCode v2.2.0 was already installed. Its configuration was **backed up before any change**:

```
~/.config/opencode/opencode.json.bak-20261030-121904
sha256 fbd84bd7b38fdd45b80f…   (copy also in data/state/backups/)
```

The pre-existing `mcp.rea` entry was preserved untouched.

## Final configuration shape

`~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "rea": { "...": "pre-existing, unchanged" },
    "servers": {
      "khunkhaar-browser":    { "type": "local", "command": ["…/.venv/bin/python", "…/mcp-servers/browser/server.py"],    "environment": { "KHUNKHAAR_ROOT": "…" } },
      "khunkhaar-api":        { "type": "local", "command": ["…/.venv/bin/python", "…/mcp-servers/api/server.py"],        "environment": { "KHUNKHAAR_ROOT": "…" } },
      "khunkhaar-database":   { "type": "local", "command": ["…/.venv/bin/python", "…/mcp-servers/database/server.py"],   "environment": { "KHUNKHAAR_ROOT": "…" } },
      "khunkhaar-filesystem": { "type": "local", "command": ["…/.venv/bin/python", "…/mcp-servers/filesystem/server.py"], "environment": { "KHUNKHAAR_ROOT": "…" } }
    }
  },
  "agent": {
    "khunkhaar-qa": {
      "description": "KHUNKHAAR QA - QA engineering agent using the khunkhaar MCP servers",
      "mode": "primary",
      "prompt": "You are the KHUNKHAAR QA agent. …"
    }
  }
}
```

Notes verified against the installed binary:

- OpenCode accepts the flat `mcp.<name>` form (used by `rea`) **and** normalizes it to
  `mcp.servers.<name>`. The KHUNKHAAR servers were registered with
  `opencode mcp add --global <name> --env KHUNKHAAR_ROOT=… -- <command>` and land in
  `mcp.servers`.
- A markdown agent file in `~/.config/opencode/agent/` was **not** picked up by v2.2.0; a
  config-level `agent.<id>` entry was, confirmed with `opencode debug agents`.

## Verification

```
$ opencode mcp list
khunkhaar-api         connected
khunkhaar-browser     connected
khunkhaar-database    connected
khunkhaar-filesystem  connected
rea                   connected

$ opencode debug agents
… khunkhaar-qa (primary) …

$ khunkhaar --doctor | grep opencode-mcp
opencode-mcp   ok   4/4   khunkhaar-browser, khunkhaar-api, khunkhaar-database, khunkhaar-filesystem
```

`--doctor` reads the config file and reports the exact missing names when one is absent.

## Launching and selecting the agent

- `khunkhaar` (from any project) runs discovery and then
  `opencode --standalone --prompt "<project context>" <project-root>`.
- Use `KHUNKHAAR_OPENCODE_ARGS` to pass extra OpenCode arguments.
- `khunkhaar --print-launch` prints the exact argv without executing it.
- The interactive TUI has **no documented `--agent` flag** in v2.2.0. For non-interactive runs
  select the agent explicitly:

```bash
opencode run --agent khunkhaar-qa "smoke-test the login page"
```

Inside the TUI, `khunkhaar-qa` is a primary agent and can be chosen with the agent switcher.

## Uninstalling the integration

The original backup is byte-identical to the pre-change file:

```bash
cp ~/.config/opencode/opencode.json.bak-20261030-121904 ~/.config/opencode/opencode.json
```

That restores `mcp.rea` and removes the four servers and the agent. The rest of
`~/KHUNKHAAR-QA` is unaffected.
