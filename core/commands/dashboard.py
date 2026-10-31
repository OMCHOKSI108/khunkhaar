"""`khunkhaar dashboard` - serve the private read-only dashboard.

The default bind is 127.0.0.1. Serving any other interface needs the explicit
`--allow-non-loopback` flag and prints a warning, because the token is then the only gate in front
of a page that can display repository paths and screenshots. Nothing here starts a scan or a test.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys

from core.shared import paths
from core.shared.config import current


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_port(raw: int | None) -> int:
    if raw is None:
        raw = int(current().get("dashboard", "port", 8899))
    if not 1024 <= int(raw) <= 65535:
        raise ValueError("the dashboard port must be between 1024 and 65535")
    return int(raw)


def run_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar dashboard start")
    parser.add_argument("--port", type=int, help="loopback port (default: dashboard.port)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--allow-non-loopback", action="store_true",
                        help="explicitly allow binding an interface other than loopback")
    args = parser.parse_args(argv)

    if not _is_loopback(args.host) and not args.allow_non_loopback:
        print(f"khunkhaar dashboard start: refusing to bind {args.host}. The dashboard is part of "
              f"a private local system; pass --allow-non-loopback if you really mean to expose it "
              f"beyond this machine.", file=sys.stderr)
        return 2
    if not _is_loopback(args.host):
        print(f"warning: binding {args.host} exposes the dashboard beyond this machine. The "
              f"access token is the only gate; do not use this on an untrusted network.",
              file=sys.stderr)
    try:
        port = _resolve_port(args.port)
    except ValueError as exc:
        print(f"khunkhaar dashboard start: {exc}", file=sys.stderr)
        return 2

    from dashboard import auth
    from dashboard.app import create_app

    try:
        token = auth.ensure_token()
    except auth.DashboardAuthError as exc:
        print(f"khunkhaar dashboard start: {exc}", file=sys.stderr)
        return 2

    app = create_app(token=token)
    shown_host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"dashboard URL   http://{shown_host}:{port}/")
    print(f"token file      {auth.token_path()} (mode 0600; the value is never printed)")
    print(f"scope           read-only over {paths.metadata_db()} and the evidence index")
    print("installed       nothing; this process serves until it is stopped")
    print("boundary        the dashboard cannot start a scan, a test or a browser session")
    try:
        import uvicorn

        uvicorn.run(app, host=args.host, port=port, log_level="warning", access_log=False)
    except ImportError:
        print("khunkhaar dashboard start: uvicorn is not installed in this environment",
              file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"khunkhaar dashboard start: could not bind {args.host}:{port}: {exc}",
              file=sys.stderr)
        return 2
    return 0
