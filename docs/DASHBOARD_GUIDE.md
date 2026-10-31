# Dashboard guide

The dashboard is a private, read-only web view of the metadata store and evidence index. It binds
loopback by default, requires a token, and cannot start a scan, a plan, a test or a browser
session.

## Start and stop

```bash
khunkhaar dashboard start
# dashboard URL   http://127.0.0.1:8899/
# token file      .../data/profiles/dashboard/token (mode 0600; the value is never printed)

khunkhaar dashboard start --port 8899
```

The process serves until it is stopped with `Ctrl+C`. Nothing is installed as a service.

On first start a token is generated with `secrets.token_urlsafe(32)` and written to
`data/profiles/dashboard/token` with mode 0600; the directory is tightened to 0700. The value is
never printed, logged or placed in an artifact. Read it yourself when you need to log in:

```bash
cat "${KHUNKHAAR_ROOT:-$HOME/KHUNKHAAR-QA}/data/profiles/dashboard/token"
```

To rotate the token, stop the process, remove the file and start again. All existing sessions
become invalid. A token file that is a symlink or shorter than 32 characters is refused instead of
silently replaced.

## Log in

Open the printed URL, paste the token into the form and submit. A successful login sets the
`khunkhaar_dashboard` cookie:

- value is an HMAC of the issue time keyed by the token, **not** the token itself;
- `HttpOnly`, `SameSite=Strict`, path `/`, 8-hour lifetime;
- `secure=False` because the default transport is plain HTTP on loopback. Do not expose the
  dashboard over a network without TLS.

`POST /logout` clears the cookie. Every request except `/login` requires a valid session; an
unauthenticated page request receives the "Authentication required" page with status 401.

## Pages

| Path | Contents |
| --- | --- |
| `/` | Overall totals, per-project cards, risk trend, agent performance, recent executions |
| `/project/<project-id>` | Latest change set, impact and risk, risk trend, critical workflows and documented invariants, most depended-on modules, findings, regression history, recent runs |
| `/execution/<run-id>` | Run identity, agents and mission counts, budget, verification results, findings, screenshots, logs, traces, tool health, report references |
| `/diff/<project-id>` | Changed files, impact and risk, the stored plan, and the run outcome for the latest change set |
| `/evidence/<ref-id>` | One indexed artifact |

All pages are rendered server-side from SQLite and the evidence index. There is no JavaScript
application, no API schema and no websocket. The dashboard shows the store as of the request; it
does not refresh itself.

## Evidence serving

`/evidence/<ref-id>` resolves the stored relative path against the KHUNKHAAR root and refuses
anything outside `evidence/` or `reports/`, including absolute paths and traversal. Content is then
served by type:

- images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) inline with the matching media type;
- text-like files as redacted `text/plain; charset=utf-8`, truncated with
  `X-KHUNKHAAR-Truncated: true`;
- everything else as an `application/octet-stream` attachment.

No artifact is served as HTML or JavaScript. Every response carries:

```
Cache-Control: no-store
Pragma: no-cache
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
                         img-src 'self' data:; form-action 'self';
                         base-uri 'none'; frame-ancestors 'none'
```

## Binding beyond loopback

```bash
khunkhaar dashboard start --host 0.0.0.0 --allow-non-loopback
```

The flag is required; without it the command refuses with exit code 2. When it is passed, a warning
is printed: the token is then the only gate in front of repository paths and screenshots, and the
session cookie is not `Secure`. Prefer an SSH tunnel or a local reverse proxy with TLS if you need
remote access. Multi-user accounts, per-user permissions and audit logs do not exist.

## What the dashboard cannot do

- Build, approve or execute a plan. Approval requires a terminal command and records a name.
- Start a scan, a regression test or a monitoring run.
- Modify findings, plans, schedules or authorizations.
- Read files outside the evidence and reports roots.
- Show a token, a credential value or `.env` content.

No page or route writes to the store. The application writes the authentication cookie and creates
the token file on first start; startup also runs the normal schema initializer, which can create
missing tables when the database is new.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| "That token did not match." | Re-read the token file and paste the value exactly; a rotated token invalidates old sessions |
| Refusing to bind 0.0.0.0 | Expected; add `--allow-non-loopback` only if you mean to expose it |
| Could not bind host:port | Another process owns the port; choose `--port` between 1024 and 65535 |
| Token file is a symlink / too short | The dashboard refuses a suspicious file; inspect it, then remove it to generate a new token |
| Empty pages | No registered projects, runs or evidence yet; run `khunkhaar project register` and a QA command first |
| 404 on an evidence link | The artifact was removed or the index is stale; run `khunkhaar retention index --rebuild` |
| Sessions expire after 8 hours | Log in again; the TTL is fixed |

## Verification status

The dashboard is covered by `tests/phase3/test_dashboard.py` and the Phase 3 acceptance chain
through the ASGI test client: login, token mismatch, session expiry, security headers, read-only
boundary, evidence content types, redaction, path confinement and 404 behavior. It was not manually
exercised in a desktop browser in this environment. See
[PHASE3_KNOWN_LIMITATIONS.md](PHASE3_KNOWN_LIMITATIONS.md).
