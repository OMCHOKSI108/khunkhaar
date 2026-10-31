# Project discovery

`khunkhaar` identifies the project it is run from and stores a non-sensitive profile. Discovery
never copies source code into `~/KHUNKHAAR-QA`, never reads secret values, and never starts the
project.

## Root detection

Starting at the current directory and walking up, the first directory containing any of these
markers is the project root:

`.git`, `package.json`, `pyproject.toml`, `setup.py`, `setup.cfg`, `go.mod`, `Cargo.toml`,
`pom.xml`, `build.gradle(.kts)`, `composer.json`, `Gemfile`, `mix.exs`,
`docker-compose.yml|yaml`, `compose.yml|yaml`.

If no marker exists, `khunkhaar` says so and exits with code 2 instead of inventing a project.

## What is collected

| Field | Source | Notes |
| --- | --- | --- |
| `languages` | file extensions during the bounded walk | counts per language |
| `frameworks` | package.json deps, pyproject/requirements, pom, gradle, go.mod, Gemfile, composer | names only |
| `project_type` | marker heuristics | e.g. Laravel/Rails → `web-app` |
| `infrastructure` | compose services/images, Dockerfile presence, DB hints | no credentials |
| `env_variable_names` | `.env`, `.env.local`, `.env.development`, `.env.test`, `.env.production`, `.env.example`, `.env.sample` | **names only** |
| `env_values_present` | same files | boolean "the value is non-empty" only |
| `api_definitions` | OpenAPI/Swagger, GraphQL, Postman, Bruno files | paths |
| `existing_tests` | test-file patterns | framework guess + count |
| `git` | branch, dirty flag, last commit subject/time | read-only `git` calls |
| `scan` | files scanned, cap, truncated flag | cap is 40 000 files |

The walk skips `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.next`,
`.nuxt`, `target`, `vendor`, caches, `site-packages`, and any extra directories listed in
`project_discovery.skip_dirs` in `config/khunkhaar.yaml`.

## What is never collected

- Values of any environment variable (the code reduces each to `bool(value.strip())`).
- Contents of `.env` files beyond name/value-presence.
- File contents of the application (only names, sizes and line counts for languages).
- Credentials from project files. Discovery does not import the credential resolver.

## Storage

- `projects/registry/<project-id>/project-profile.json` — mode 0600.
- `<project-id>` = slug of the directory name + first 8 hex chars of the SHA-256 of the absolute
  root path, e.g. `webapp-92cf5dbe`.
- SQLite `data/metadata/platform.sqlite3` (WAL, mode 0600): `projects`, `discovery_snapshots`,
  `tool_health`, `qa_runs`, `report_refs`, `evidence_refs`. No credential columns exist.

Two projects are registered from this install:

| Project | ID | Type | Files scanned |
| --- | --- | --- | --- |
| web-app (`~/workspace/webapp`) | `webapp-92cf5dbe` | web-app (Laravel, PostgreSQL+Redis) | 651 |
| code-audit (`~/workspace/code-audit`) | `codeaudit-5f1843dc` | unknown (Python/TS/JS) | bounded walk |

## Isolation audit

`scripts/validate_project_isolation.py` proves that no `.env` value is carried into a profile:

- it scans every registered profile for values taken from that project's `.env*` files;
- a match is only reported as a leak when **no non-.env project file** contains the same string
  (otherwise the string came from shared infrastructure, e.g. a compose image named `postgres`);
- it also fails on any profile key that looks like it stores raw values, and on cross-project
  value bleed.

Last result: **2 profiles checked, no leaks** — `reports/validation/project_isolation.txt`.
The earlier warning about `DB_USERNAME`/`DB_PASSWORD` in the reference project was a false
positive: both values are the literal word `postgres`, which appears in `docker-compose.yml`.

## Cleanup policy

`runtime/tmp` is disposable and enforced by `fs_tmp_cleanup` with a TTL. Registry profiles are
kept until the operator deletes them; they contain no secrets.
