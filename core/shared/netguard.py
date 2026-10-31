"""Target authorization for outbound requests (SSRF and scope control).

Every request made through the API Call Tool passes here first. A target is allowed only if:
  * the scheme is http/https,
  * the host matches an explicit allowlist entry,
  * the host is not on the deny list, and
  * every resolved address passes the private/link-local policy.

Redirects are followed manually so each hop is re-validated against the same rules; otherwise a
permitted host could bounce a request to an internal endpoint.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config

# RFC1918 + loopback + link-local + unique-local, used for the private-range policy.
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class Decision:
    allowed: bool
    reason: str
    host: str = ""
    port: int | None = None
    scheme: str = ""


def host_matches(entry: str, host: str, port: int | None) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False
    entry_host = entry
    entry_port = None
    m = re.match(r"^(?P<h>\[[0-9a-f:.]+\]|[a-z0-9._-]+)?(?::(?P<p>\d+))?$", entry)
    if m:
        entry_host = (m.group("h") or entry).lower()
        if m.group("p"):
            entry_port = int(m.group("p"))
    if entry_port is not None and port is not None and entry_port != port:
        return False
    if entry_host.startswith("*."):
        suffix = entry_host[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix[1:]
    return host == entry_host


def normalize_host(hostname: str) -> str:
    host = (hostname or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host.rstrip(".")


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def _ip_is_denied(ip: str, allow_private: bool) -> str | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"unparseable address {ip}"
    # Link-local / cloud metadata is never allowed, regardless of the private-range policy.
    if addr.is_link_local or ip.startswith("169.254."):
        return f"link-local/metadata address {ip}"
    if addr.is_unspecified:
        return f"unspecified address {ip}"
    if not allow_private:
        for net in _PRIVATE_NETS:
            if addr in net:
                return f"private-range address {ip} is outside policy"
    return None


def check_target(url: str, cfg: Config) -> Decision:
    api = cfg.section("api")
    allow = api.get("allowed_hosts") or []
    deny = [str(d).lower() for d in (api.get("denied_hosts") or [])]
    allow_private = bool(api.get("allow_private_ranges", True))

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return Decision(False, f"scheme '{scheme or 'none'}' is not http/https", scheme=scheme)

    try:
        port = parts.port
    except ValueError:
        return Decision(False, "invalid port in URL", scheme=scheme)

    host = normalize_host(parts.hostname or "")
    if not host:
        return Decision(False, "URL has no host", scheme=scheme)

    effective_port = port or (443 if scheme == "https" else 80)

    if any(host_matches(d, host, effective_port) for d in deny):
        return Decision(False, f"host '{host}' is explicitly denied", host, effective_port, scheme)

    if not allow:
        return Decision(
            False,
            "no target allowlist is configured - add hosts to config/khunkhaar.yaml "
            "under api.allowed_hosts before making requests",
            host, effective_port, scheme,
        )

    if not any(host_matches(a, host, port) for a in allow):
        return Decision(
            False,
            f"host '{host}' is not in api.allowed_hosts",
            host, effective_port, scheme,
        )

    if re.fullmatch(r"[0-9a-f:.]+", host.replace("%", "")) and any(c.isdigit() for c in host):
        ips = [host]  # literal IP address, no DNS needed
    else:
        ips = _resolve(host)
        if not ips:
            return Decision(False, f"host '{host}' does not resolve", host, effective_port, scheme)

    for ip in ips:
        problem = _ip_is_denied(ip, allow_private)
        if problem:
            return Decision(False, f"host '{host}' resolves to {problem}", host, effective_port, scheme)

    return Decision(True, "allowed", host, effective_port, scheme)
