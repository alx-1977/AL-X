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
from urllib.parse import urlsplit, urlunsplit

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


def is_public_address(value: str) -> bool:
    """Report whether one literal address is on the public internet.

    An IPv4-mapped IPv6 address is unwrapped before classification. Without
    that, `::ffff:127.0.0.1` presents as an ordinary global IPv6 address and
    every loopback rule above is bypassed by spelling.
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
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


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

    normalised = urlunsplit(
        (scheme, parts.netloc, parts.path or "/", parts.query, "")
    )
    return PublicUrl(normalised, scheme, host, port, addresses)


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
