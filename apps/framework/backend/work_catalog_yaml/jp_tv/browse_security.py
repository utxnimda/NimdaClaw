from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def _authority_host_port(authority: str, *, scheme: str) -> tuple[str, int | None] | None:
    """Parse an HTTP authority without treating an IPv6 colon as a port separator."""
    try:
        parsed = urlsplit(f"//{str(authority or '').strip()}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.username is not None or parsed.password is not None:
        return None
    if port is None:
        port = 443 if scheme.casefold() == "https" else 80
    return hostname, port


def is_loopback_hostname(hostname: str | None) -> bool:
    """Return whether a bind/request hostname is explicitly local-only."""
    raw = str(hostname or "").strip().strip("[]")
    if not raw:
        return False
    if raw.casefold() == "localhost":
        return True
    try:
        return ip_address(raw).is_loopback
    except ValueError:
        return False


def origin_matches_request(
    origin: str,
    *,
    request_scheme: str,
    request_host: str,
) -> bool:
    """Check a browser Origin header against the current request origin."""
    try:
        parsed = urlsplit(str(origin or "").strip())
        origin_port = parsed.port
    except ValueError:
        return False
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        return False
    if scheme != str(request_scheme or "").casefold():
        return False
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False

    request_authority = _authority_host_port(request_host, scheme=scheme)
    if request_authority is None:
        return False
    request_hostname, request_port = request_authority
    if origin_port is None:
        origin_port = 443 if scheme == "https" else 80
    if origin_port != request_port:
        return False

    origin_hostname = parsed.hostname
    return origin_hostname.casefold() == request_hostname.casefold() or (
        is_loopback_hostname(origin_hostname) and is_loopback_hostname(request_hostname)
    )


def mutation_source_is_allowed(
    *,
    fetch_site: str,
    origin: str,
    request_scheme: str,
    request_host: str,
) -> bool:
    """Validate a mutation using browser Fetch Metadata and an Origin fallback.

    ``Sec-Fetch-Site`` is a forbidden browser request header, so page
    JavaScript cannot forge ``same-origin``. Older clients and intermediaries
    that omit Fetch Metadata continue through the strict Origin comparison.
    """
    site = str(fetch_site or "").strip().casefold()
    if site == "cross-site":
        return False
    if site == "same-origin":
        return True
    return not origin or origin_matches_request(
        origin,
        request_scheme=request_scheme,
        request_host=request_host,
    )
