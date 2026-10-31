# Troubleshooting

Problems actually hit during installation, and the fix that was applied.

## `khunkhaar --doctor` says `playwright-webkit: unavailable`

WebKit needs system libraries. The launcher reported `libavif.so.16: cannot open shared object`.

```bash
bash scripts/install_webkit_deps.sh          # no root, vendors libs into the WebKit bundle
bash scripts/install_webkit_deps.sh --system # apt path if you have sudo
khunkhaar --doctor
```

Do not set `LD_LIBRARY_PATH` by hand: Playwright's `minibrowser-wpe/MiniBrowser` wrapper
overwrites it, which is why the libraries must live in the bundle's `sys/lib`.

## A new Playwright version breaks WebKit again

A `playwright install` that creates a new `webkit-<build>` directory starts with an empty
`sys/lib`. Re-run `scripts/install_webkit_deps.sh`.

## `khunkhaar --doctor` says `postgres-test-profile: needs-env`

Expected in a bare shell: the password is loaded from `data/profiles/db/*.env` when `khunkhaar`
launches. To check the DB from a shell manually:

```bash
set -a; . data/profiles/db/khunkhaar_test_pg.env; set +a
khunkhaar --doctor | grep postgres
```

## `opencode mcp list` shows fewer servers than expected

Compare against the config:

```bash
python3 -c "import json,os;print(list(json.load(open(os.path.expanduser('~/.config/opencode/opencode.json')))['mcp']['servers']))"
opencode debug config | head -40
```

Note that `opencode mcp add` writes to `mcp.servers` while older entries (e.g. `rea`) may stay
flat under `mcp`. Both forms are valid and are normalized by OpenCode.

## The TUI does not accept `--agent`

OpenCode v2.2.0 does not document `--agent` for the interactive TUI. Use
`opencode run --agent khunkhaar-qa "<prompt>"` for scripted runs, or pick the primary agent in
the TUI switcher.

## Mobile viewport looks wrong (980 px layout width)

Playwright reports the CSS layout viewport, not the visual viewport. Without
`<meta name="viewport" content="width=device-width, initial-scale=1">` a mobile page lays out at
980 px. That is correct browser behaviour, not an emulation failure; the device viewport and DPR
are still applied (verified: iPhone 13 → 390×664, DPR 3).

## ZAP scan fails or the image seems missing

- The image is pulled on first use; the pull can take minutes.
- Do not judge a pull by a piped command's exit status (`docker pull … | tail` reports the exit
  code of `tail`). Run `docker pull ghcr.io/zaproxy/zaproxy:stable` directly once.
- The wrapper refuses non-loopback targets unless `--allow-remote` **and** the host is listed in
  `security.destructive_approved_targets`. That refusal is intended.

## Mock API returns 500 on some requests

Fixed: `POST/PUT/PATCH /items` now return a documented 400 for malformed or non-object JSON
(`_json_object` in `tools/api/mock_server.py`). Restart the mock after editing:

```bash
pkill -f mock_server.py
.venv/bin/python tools/api/mock_server.py &
```

## Tests cannot import `mcp-servers/...`

The directory name contains a hyphen, so it is not importable. `tests/conftest.py` loads each
server with `importlib.util.spec_from_file_location` (`load_server_module`); use that helper.

## `khunkhaar --project-info` reports nothing

The directory has no project marker (`.git`, `package.json`, `pyproject.toml`, …). Run it in a
project root, or add a marker. Discovery deliberately refuses to guess.

## A profile contains a word that equals an `.env` value

Check with the isolation audit, which distinguishes real leaks from shared vocabulary:

```bash
.venv/bin/python scripts/validate_project_isolation.py
```

Example: a project's `DB_USERNAME`/`DB_PASSWORD` values match the word `postgres`, and the
profile contains `postgres:17-alpine` from `docker-compose.yml`. That is not a leak.

## Port already in use

- Mock API: `127.0.0.1:8731`
- Disposable PostgreSQL: `127.0.0.1:5445`

Find the owner with `ss -ltnp | grep <port>`; do not kill unrelated services. The test fixture
reuses an already-running mock instead of failing.

## `opencode mcp list` says "No MCP servers configured"

On a cold start `opencode mcp list --standalone` can answer before the private server has
loaded its MCP registry, even though the server does spawn the servers (check its own log, or
query the warmed background service). Verify with the background service from a project:

```bash
cd /path/to/project
opencode mcp list          # first call may race; run it again
```

All four `khunkhaar-*` servers should read `connected`. The authoritative check is an
end-to-end call: `reports/validation/opencode_e2e.txt` shows an agent calling
`khunkhaar-api.api_request` and getting a real 200 from the mock.

## `khunkhaar-qa` agent is not preselected in the TUI

The OpenCode 2.2.0 TUI has no `--agent` flag. `khunkhaar` passes the project context as the
initial prompt and all MCP servers are global, so tools are available either way. Select
`khunkhaar-qa` in the session, or run non-interactively with
`opencode run --agent khunkhaar-qa`. The global `default_agent` is intentionally not changed.
