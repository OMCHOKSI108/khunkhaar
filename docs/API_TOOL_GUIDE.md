# API Call Tool guide

`khunkhaar-api` executes HTTP requests safely and returns redacted, size-capped results.

## Policy gates (config/khunkhaar.yaml)

| Gate | Default | Behaviour |
| --- | --- | --- |
| `api.allowed_hosts` | `127.0.0.1:8731`, `localhost:8731` | every request is refused unless host **and port** match |
| `api.denied_hosts` | metadata addresses | always refused, even if allowlisted |
| `allow_private_ranges` | `true` | private LAN targets allowed on this dev machine |
| timeouts | 10 s, max 120 s | caller value is clamped |
| retries | 3 | only for configured transient statuses |
| size caps | 2 MB request, 5 MB response, 20 k chars shown | larger responses are truncated and flagged |
| methods | GET/HEAD/OPTIONS safe; POST/PUT/PATCH mutating; DELETE destructive | see below |

### Destructive methods (DELETE)

Two independent conditions are required:

1. the caller passes `allow_destructive=true`, **and**
2. the request host is listed in `security.destructive_approved_targets` (currently empty).

The tool's error message states exactly which condition failed. A caller flag alone is never
treated as approval.

### SSRF and redirects

Every redirect hop is re-checked by `core/shared/netguard.py`: link-local, metadata and
non-http(s) targets are refused even when the original host was allowed.

## Credentials

Auth profiles never accept a password argument. Resolution order:

1. environment variable named by the profile (`env_var` in config), or
2. a 0600 JSON file at `data/profiles/auth/<profile>.json` written by the operator CLI.

`core/shared/credentials.py` deliberately does not read credentials found in project files.

## Redaction

Redaction is structural (by key name: `authorization`, `cookie`, `api_token`, `client_secret`,
`credential`, …) plus textual (connection strings, `ghp_…`, `xoxb-…`, AWS keys, JWTs). Values
that are booleans/numbers are left intact so presence flags survive. Headers, bodies, query
strings and persisted evidence all pass through the same functions.

## Typical calls

```jsonc
// health check
{"tool": "api_request", "arguments": {"method": "GET", "url": "http://127.0.0.1:8731/health"}}

// JSON body with a timeout
{"tool": "api_request", "arguments": {"method": "POST", "url": "http://127.0.0.1:8731/items",
  "json": {"name": "widget"}, "timeout_seconds": 20}}

// discover an OpenAPI document before writing requests by hand
{"tool": "api_discover_openapi", "arguments": {"url": "http://127.0.0.1:8731/openapi.json"}}

// GraphQL
{"tool": "api_graphql", "arguments": {"url": "http://127.0.0.1:8731/graphql",
  "query": "{ hello }"}}
```

## Validation evidence

- 42 API tests: `reports/validation/api_tests.txt`
- Schemathesis property run against the mock (11 operations, 142 cases, 0 failures):
  `reports/security/schemathesis.txt`
- All runs target the local mock only; **no production API was contacted.** The mock lives in
  `tools/api/mock_server.py` and binds to `127.0.0.1`.
