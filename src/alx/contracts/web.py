"""Contracts for reading one public web page, under D-025.

Retrieval is mechanical. Nothing here ranks a source, prefers a domain,
summarises a page, or decides that a result is worth having: those are AL/X's
judgements, and D-025 keeps them in the Core. What this module defines is the
shape of a public URL that has been proven safe to fetch, the shape of what
comes back, and the vocabulary of refusals.

Every refusal is a fact returned to AL/X, never a conclusion drawn for her. A
blocked page and an empty page are different things, and code that collapsed
them would be deciding what the retrieval meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


# D-025 resource bounds. These are mechanical context and cost limits: the
# Core's input is finite, and an autonomous turn refuses rather than truncates
# when its ceiling is exceeded. They say nothing about which content matters.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 8_000
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})

# D-025 declares only textual content types readable in V1.
ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)


# The refusals AL/X may receive. Each names a distinct fact about the world so
# she can tell "there is nothing there" from "I was not allowed to look".
URL_NOT_PUBLIC = "url_not_public"
UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
RETRIEVAL_BLOCKED = "retrieval_blocked"
RETRIEVAL_TIMEOUT = "retrieval_timeout"
UNSUPPORTED_DYNAMIC_PAGE = "unsupported_dynamic_page"
PAGE_TOO_LARGE = "page_too_large"
PROVIDER_FAILED = "provider_failed"

WEB_FETCH_FAILURES = (
    URL_NOT_PUBLIC,
    UNSUPPORTED_CONTENT_TYPE,
    RETRIEVAL_BLOCKED,
    RETRIEVAL_TIMEOUT,
    UNSUPPORTED_DYNAMIC_PAGE,
    PAGE_TOO_LARGE,
    PROVIDER_FAILED,
)


class WebRetrievalError(Exception):
    """A refusal carrying one declared code and no retrieved content.

    The code travels; the page does not. A failure that carried fragments of
    what it failed to read would put unvalidated bytes on the failure channel,
    where nothing is expecting untrusted content.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in WEB_FETCH_FAILURES:
            raise ValueError(f"undeclared web failure code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublicAddress:
    """One resolved address that passed every D-025 public-address rule."""

    literal: str
    family: int

    def __post_init__(self) -> None:
        if not self.literal.strip():
            raise ValueError("a validated address must not be blank")


@dataclass(frozen=True, slots=True)
class PublicUrl:
    """A URL proven safe to request, with the addresses it resolved to.

    Holding the hostname and the validated addresses together is what makes
    the connection honest: the socket goes to `addresses`, while TLS and the
    `Host` header keep using `host`. Splitting them across call sites is how a
    pinned connection quietly loses its certificate check.
    """

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[PublicAddress, ...]

    def __post_init__(self) -> None:
        if self.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"scheme is not public web: {self.scheme}")
        if self.port not in ALLOWED_PORTS:
            raise ValueError(f"port is not a public web port: {self.port}")
        if not self.host.strip():
            raise ValueError("host must not be blank")
        if not self.addresses:
            raise ValueError("a public URL requires at least one validated address")

    @property
    def source_domain(self) -> str:
        return self.host


@dataclass(frozen=True, slots=True)
class WebPage:
    """One retrieved public page and the provenance that makes it citable.

    `requested_url` and `final_url` are both kept because they answer different
    questions: what AL/X asked for, and what actually answered. A redirect that
    silently replaced the first with the second would erase the fact that one
    happened.
    """

    requested_url: str
    final_url: str
    source_domain: str
    retrieved_at: datetime
    http_status: int
    content: str
    content_omitted_characters: int = 0
    title: str | None = None
    publisher: str | None = None

    def __post_init__(self) -> None:
        if not self.requested_url.strip() or not self.final_url.strip():
            raise ValueError("a retrieved page requires both URLs")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone aware")
        if self.content_omitted_characters < 0:
            raise ValueError("omitted characters cannot be negative")
        if len(self.content) > MAX_EXTRACTED_CHARACTERS:
            raise ValueError(
                "extracted content exceeds the D-025 bound; it must be cut "
                "with the shortfall reported, never returned whole"
            )


class WebFetchProvider(Protocol):
    """Retrieves exactly one public page, or raises a declared refusal."""

    def fetch(self, url: str, max_characters: int) -> WebPage: ...


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "MAX_DOWNLOAD_BYTES",
    "MAX_EXTRACTED_CHARACTERS",
    "MAX_REDIRECTS",
    "PAGE_TOO_LARGE",
    "PROVIDER_FAILED",
    "PublicAddress",
    "PublicUrl",
    "RETRIEVAL_BLOCKED",
    "RETRIEVAL_TIMEOUT",
    "UNSUPPORTED_CONTENT_TYPE",
    "UNSUPPORTED_DYNAMIC_PAGE",
    "URL_NOT_PUBLIC",
    "WEB_FETCH_FAILURES",
    "WebFetchProvider",
    "WebPage",
    "WebRetrievalError",
]
