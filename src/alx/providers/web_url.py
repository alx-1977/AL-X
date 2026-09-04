"""The D-025 public-web boundary: what may be connected to, and to what address.

This module is the whole reason a public-web reader does not become arbitrary
internal-network access. It decides one mechanical question — is this
destination on the public internet — and it decides nothing else. It never
looks at what a page is about, who publishes it, or whether it is worth
reading.

Two properties matter more than the individual rules:

Every resolved address must pass. A hostname answering with one public and one
private address is refused outright rather than connected to on the public one.
An attacker who controls a DNS answer controls which address is used, so
choosing the acceptable member of a mixed set validates nothing.

Resolution happens exactly once, here, and the caller connects to the literal
address this module returned. A design that validated a hostname and then let
the socket layer resolve it again would leave the rebinding window wide open:
the name checked and the name connected to would be two separate lookups.

No network I/O happens in this module. `getaddrinfo` is injected, so the entire
boundary is provable offline.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from alx.contracts.web import (
    ALLOWED_PORTS,
    ALLOWED_SCHEMES,
    URL_NOT_PUBLIC,
    PublicAddress,
    PublicUrl,
    WebRetrievalError,
)


# Injected so the boundary is testable without a network and so production has
# exactly one resolution site.
Resolver = Callable[[str, int], list[tuple[Any, ...]]]

DEFAULT_PORTS = {"http": 80, "https": 443}


def _refuse(detail: str) -> WebRetrievalError:
    return WebRetrievalError(URL_NOT_PUBLIC, detail)


# Ranges Python's own flags do not refuse, or refuse inconsistently between
# versions. Each is a real destination that is not the public internet, so
# each is named explicitly rather than left to `is_global` to catch.
_EXCLUDED_NETWORKS = (
    # RFC 6598 carrier-grade NAT. Neither private nor global to Python, and
    # routable straight into a carrier's or a hosting provider's own network.
    ipaddress.ip_network("100.64.0.0/10"),
    # Deprecated 6to4 relay anycast: is_global is True, and it is a tunnel.
    ipaddress.ip_network("192.88.99.0/24"),
    # Benchmarking range, frequently wired to internal test infrastructure.
    ipaddress.ip_network("198.18.0.0/15"),
    # Documentation ranges: never a real public destination.
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
    # NAT64 well-known prefix: an embedded IPv4 destination behind a translator.
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    # ORCHID / deprecated ORCHID: not routable destinations.
    ipaddress.ip_network("2001:10::/28"),
    ipaddress.ip_network("2001:20::/28"),
)


def is_public_address(value: str) -> bool:
    """Report whether one literal address is on the public internet.

    Acceptance requires two things, not one: the address must be globally
    routable *and* outside every category this boundary excludes. Neither test
    is sufficient alone. `is_global` misses RFC 6598 shared space, which is a
    real reachable network; and it is True for multicast and for the 6to4
    relay prefix, so it cannot simply replace the category flags either.

    Embedded IPv4 addresses are unwrapped before classification. Without that,
    `::ffff:127.0.0.1` presents as an ordinary global IPv6 address and every
    loopback rule is bypassed by spelling alone.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Both spellings of an embedded IPv4 address are unwrapped: a mapped
        # or 6to4 address that stayed wrapped would be classified as global.
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.sixtofour is not None:
            address = address.sixtofour
        elif getattr(address, "teredo", None) is not None:
            # Teredo carries the server address in the high bits; the tunnel
            # can reach anything, so it is refused rather than unwrapped.
            return False
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if any(address in network for network in _EXCLUDED_NETWORKS
           if network.version == address.version):
        return False
    return address.is_global


def _validated_addresses(
    host: str, port: int, resolver: Resolver
) -> tuple[PublicAddress, ...]:
    """Resolve once and require every answer to be public."""
    try:
        answers = resolver(host, port)
    except Exception as error:
        raise _refuse(f"resolution failed: {type(error).__name__}") from None
    if not answers:
        raise _refuse("hostname resolved to no address")

    addresses: list[PublicAddress] = []
    seen: set[str] = set()
    for answer in answers:
        try:
            family = answer[0]
            literal = answer[4][0]
        except (IndexError, TypeError):
            raise _refuse("resolver returned an unusable answer") from None
        if not is_public_address(literal):
            # The whole hostname is refused, not this one address. Selecting
            # the public member of a mixed answer would let whoever controls
            # the DNS response choose the destination.
            raise _refuse("hostname resolves to a non-public address")
        if literal in seen:
            continue
        seen.add(literal)
        addresses.append(PublicAddress(literal, family))
    if not addresses:
        raise _refuse("hostname resolved to no usable address")
    return tuple(addresses)


def system_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
    """The one production resolution site."""
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


def parse_public_url(raw: str, resolver: Resolver | None = None) -> PublicUrl:
    """Validate a URL and resolve it to public addresses, or refuse it.

    Every check that can be made without the network is made first, so a
    malformed or obviously non-public URL never causes a DNS lookup.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse("blank url")
    candidate = raw.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise _refuse("url could not be parsed") from None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _refuse(f"scheme is not public web: {scheme or 'missing'}")
    if parts.username is not None or parts.password is not None:
        raise _refuse("credentials in url")
    # `urlsplit` keeps everything before an "@" in netloc as credentials, so a
    # bare "@" that yields no username still signals a credentialed form.
    if "@" in parts.netloc:
        raise _refuse("credentials in url")

    try:
        host = parts.hostname
    except ValueError:
        raise _refuse("host could not be parsed") from None
    if not host:
        raise _refuse("missing host")
    host = host.strip().rstrip(".").lower()
    if not host:
        raise _refuse("missing host")

    try:
        port = parts.port
    except ValueError:
        raise _refuse("port could not be parsed") from None
    port = DEFAULT_PORTS[scheme] if port is None else port
    if port not in ALLOWED_PORTS:
        raise _refuse(f"port is not a public web port: {port}")

    literal = _as_literal(host)
    if literal is not None:
        # A URL naming an address directly needs no resolution, but it gets
        # exactly the same classification.
        if not is_public_address(literal):
            raise _refuse("literal address is not public")
        family = (
            socket.AF_INET6
            if isinstance(ipaddress.ip_address(literal), ipaddress.IPv6Address)
            else socket.AF_INET
        )
        addresses = (PublicAddress(literal, family),)
    else:
        if "." not in host:
            # A single-label name resolves through local search domains and
            # names an internal host, not a public site.
            raise _refuse("hostname is not fully qualified")
        addresses = _validated_addresses(
            host, port, resolver or system_resolver
        )

    # Components, never a string to be edited later. The transport URL and the
    # provenance URL are both built from these, so the spelling that was
    # validated is necessarily the spelling that is used.
    return PublicUrl(
        scheme,
        host,
        port,
        addresses,
        path=parts.path or "/",
        query=f"?{parts.query}" if parts.query else "",
    )


def _as_literal(host: str) -> str | None:
    """Return the host as an IP literal, or None when it is a name."""
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


__all__ = [
    "DEFAULT_PORTS",
    "Resolver",
    "is_public_address",
    "parse_public_url",
    "system_resolver",
]
