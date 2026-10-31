#!/usr/bin/env bash
# Provides the system libraries Playwright WebKit needs, without root by default.
#
# Why vendored: on this host sudo requires an interactive password, and WebKit's launcher
# overwrites LD_LIBRARY_PATH, so the libraries must sit in the browser's own bundle.
# `--system` instead installs the same packages with apt (needs sudo/root).
#
# Usage:
#   bash scripts/install_webkit_deps.sh            # no-root vendor install (default)
#   bash scripts/install_webkit_deps.sh --system   # apt install, requires root/sudo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${ROOT}/.venv/bin/python"
PLAYWRIGHT_CACHE="${HOME}/.cache/ms-playwright"
PKGS=(libavif16 libgav1-1 libyuv0 libsharpyuv0)

verify() {
  "${VENV_PY}" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.webkit.launch(headless=True)
    page = b.new_page()
    page.set_content("<h1>webkit ok</h1>")
    print("webkit LAUNCH OK, version:", b.version, "->", page.inner_text("h1"))
    b.close()
PY
}

if [ "${1:-}" = "--system" ]; then
  APT=(apt-get)
  [ "${EUID}" -ne 0 ] && APT=(sudo apt-get)
  "${APT[@]}" update -qq
  "${APT[@]}" install -y --no-install-recommends "${PKGS[@]}"
  verify
  exit 0
fi

echo "Vendoring WebKit libraries (no root): ${PKGS[*]}"
mkdir -p "${ROOT}/opt/webkit-libs" "${ROOT}/runtime/tmp/webkit-debs"
cd "${ROOT}/runtime/tmp/webkit-debs"
apt-get download "${PKGS[@]}"
cd "${ROOT}"
for deb in runtime/tmp/webkit-debs/*.deb; do
  dpkg-deb -x "${deb}" opt/webkit-libs
done

for bundle in "${PLAYWRIGHT_CACHE}"/webkit-*/minibrowser-wpe/sys/lib "${PLAYWRIGHT_CACHE}"/webkit-*/minibrowser-gtk/sys/lib; do
  [ -d "${bundle}" ] || continue
  cp --update=none opt/webkit-libs/usr/lib/x86_64-linux-gnu/lib{avif.so.16,gav1.so.1,yuv.so.0,sharpyuv.so.0} "${bundle}/"
  echo "  installed into ${bundle}"
done

echo "Re-run this script after a Playwright update (a new webkit-* bundle starts empty)."
verify
