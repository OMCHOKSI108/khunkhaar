#!/usr/bin/env bash
# On-demand OWASP ZAP baseline scan in Docker.
#
# Authorization gate: only loopback targets run without approval. Anything else needs
# --allow-remote AND a matching entry in security.destructive_approved_targets, because an
# active scan sends attack traffic.
#
# Usage:
#   scripts/zap_baseline.sh --target http://127.0.0.1:8731
#   scripts/zap_baseline.sh --target https://staging.example --allow-remote
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="ghcr.io/zaproxy/zaproxy:stable"
TARGET=""
ALLOW_REMOTE=0
SPIDER_MINUTES=1

while [ $# -gt 0 ]; do
  case "$1" in
    -t|--target) TARGET="${2:?}"; shift 2 ;;
    --allow-remote) ALLOW_REMOTE=1; shift ;;
    --spider-minutes) SPIDER_MINUTES="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$TARGET" ] || { echo "missing --target" >&2; exit 2; }

decision="$("${ROOT}/.venv/bin/python" - "$TARGET" "$ALLOW_REMOTE" <<'PY'
import sys
from urllib.parse import urlsplit
from core.shared.config import current
from core.shared.netguard import host_matches, normalize_host

url, allow_remote = sys.argv[1], sys.argv[2] == "1"
parts = urlsplit(url)
if parts.scheme not in ("http", "https"):
    print("deny scheme")
    raise SystemExit
host = normalize_host(parts.hostname or "")
port = parts.port or (443 if parts.scheme == "https" else 80)
if host in ("127.0.0.1", "localhost", "::1"):
    print("allow loopback")
    raise SystemExit
if not allow_remote:
    print(f"deny remote {host}:{port} without --allow-remote")
    raise SystemExit
approved = [str(h).lower() for h in current().get("security", "destructive_approved_targets", []) or []]
if any(host_matches(entry, host, port) for entry in approved):
    print(f"allow approved {host}:{port}")
else:
    print(f"deny {host}:{port} is not in security.destructive_approved_targets")
PY
)"

case "$decision" in
  allow*) echo "zap: $decision" ;;
  *) echo "zap: refused - ${decision#deny }" >&2; exit 3 ;;
esac

docker image inspect "${IMAGE}" >/dev/null 2>&1 || docker pull "${IMAGE}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${ROOT}/reports/security/zap/${STAMP}"
mkdir -p "${OUT}"
echo "zap: scanning ${TARGET} -> ${OUT}"

docker run --rm --network host -v "${OUT}:/zap/wrk:rw" "${IMAGE}" \
  zap-baseline.py -t "${TARGET}" -J zap.json -r zap.html -I -m "${SPIDER_MINUTES}"

echo "zap: report ${OUT}/zap.html and ${OUT}/zap.json"
