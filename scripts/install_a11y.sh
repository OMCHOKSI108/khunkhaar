#!/usr/bin/env bash
# Vendors axe-core next to the browser tools so browser_a11y_scan works without network access.
# Version is pinned; re-run after editing AXE_VERSION.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AXE_VERSION="4.14.0"
DEST="${ROOT}/opt/axe"

npm pack "axe-core@${AXE_VERSION}" --silent --pack-destination "${ROOT}/runtime/tmp" >/dev/null
mkdir -p "${DEST}"
tar -xzf "${ROOT}/runtime/tmp/axe-core-${AXE_VERSION}.tgz" -C "${DEST}" --strip-components=1
test -s "${DEST}/axe.min.js"
echo "axe-core ${AXE_VERSION} installed at ${DEST}/axe.min.js"
