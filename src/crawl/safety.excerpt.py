# Excerpt from app/fetcher/safety.py ("# ..." marks omitted code)

"""Outbound-request safety checks (SSRF protection).

Every outbound connection is subject to two layers:

1. ``check_url_syntax`` — no network access: http/https only, a host is present, no
   embedded credentials, no private-network host names, and IP literals must be public.
   Run before a request is issued and again for every redirect target.
2. ``PinnedNetworkBackend`` — at connect time, the host is resolved once, *every*
   resolved address must be public, and the TCP connection is made to one of those
   vetted addresses. TLS still verifies the certificate against the host name (SNI).
   Because validation and connection use the same resolution result, a DNS-rebinding
   answer cannot redirect the connection to an internal address.

"Public" excludes private, loopback, link-local (including the 169.254.169.254 cloud
metadata endpoint), multicast, reserved, unspecified, CGNAT/shared space, and IPv6
transition forms (IPv4-mapped, 6to4, Teredo, NAT64) that embed such an address.

Known limitations: resolution uses the operating system resolver (``getaddrinfo``),
so ``/etc/hosts`` and local DNS configuration apply; outbound proxies are not
supported (the client ignores proxy environment variables); host-level egress
firewalling is still recommended in production as defence in depth.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import typing
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpcore

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = typing.Callable[[str, int], typing.Awaitable[tuple[IPAddress, ...]]]

ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".corp",
)
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")  # includes cloud metadata endpoints
# Addresses Python's ``is_global`` may not classify as non-global on every version.
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # shared address space / CGNAT
    _LINK_LOCAL_V4,
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDS over IPv6 (also ULA)
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
)
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# ...

def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_public_ip(embedded)
    if any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version):
        return False
    return ip.is_global and not (
        ip.is_multicast
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_allowed_ip(ip: IPAddress, policy: SafetyPolicy) -> bool:
    if is_public_ip(ip):
        return True
    # Even in test mode, never allow unspecified/multicast or the metadata range.
    return (
        policy.allow_private_addresses
        and not ip.is_unspecified
        and not ip.is_multicast
        and not (ip.version == 4 and ip in _LINK_LOCAL_V4)
    )


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def check_url_syntax(url: str, policy: SafetyPolicy = DEFAULT_POLICY) -> tuple[str, str, int]:
    """Validate a URL without network access. Returns ``(scheme, host, port)``."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {url!r}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"unsupported scheme: {scheme or '(none)'!r}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    ip = _parse_ip(host)
    if ip is None:
        if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeURLError(f"host {host!r} is not publicly routable")
        if "." not in host:
            raise UnsafeURLError(f"single-label host {host!r} is not allowed")
    elif not _is_allowed_ip(ip, policy):
        raise UnsafeURLError(f"address {ip} is not publicly routable")
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]

# ...

async def resolve_safe_addresses(
    host: str,
    port: int,
    policy: SafetyPolicy = DEFAULT_POLICY,
    resolver: Resolver = system_resolver,
) -> tuple[IPAddress, ...]:
    """Resolve ``host`` and require that *every* address is allowed."""
    host = host.strip("[]").rstrip(".").lower()
    literal = _parse_ip(host)
    if literal is None and (host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES)):
        raise UnsafeURLError(f"host {host!r} is not publicly routable")
    addresses = (literal,) if literal is not None else await resolver(host, port)
    if not addresses:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    blocked = [str(ip) for ip in addresses if not _is_allowed_ip(ip, policy)]
    if blocked:
        raise UnsafeURLError(f"host {host!r} resolves to non-public address(es): {blocked}")
    return addresses

# ...

class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to validated addresses.

    httpcore passes the origin host name here and later performs the TLS handshake with
    ``server_hostname`` set to that host name, so certificate verification is unchanged
    while the TCP connection goes to an address that passed validation.
    """

    def __init__(
        self,
        policy: SafetyPolicy = DEFAULT_POLICY,
        resolver: Resolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
        resolve_timeout: float = 5.0,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve_timeout = resolve_timeout

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(self._resolve_timeout):
                addresses = await resolve_safe_addresses(host, port, self._policy, self._resolver)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"DNS resolution of {host!r} timed out") from exc
        except DNSResolutionError as exc:
            # Surfaces as httpx.ConnectError: a retryable network failure.
            raise httpcore.ConnectError(str(exc)) from exc
        last_error: Exception | None = None
        for ip in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(ip),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error
