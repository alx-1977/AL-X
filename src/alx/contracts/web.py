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

# Every field a page controls needs its own ceiling, not just the body. A
# title is attacker-chosen text exactly as the body is, and unlike the body it
# is persisted in durable goal state, so an unbounded one would bloat the Core
# prompt and the goal store permanently. These are mechanical context and
# storage limits; they say nothing about which content matters.
MAX_TITLE_CHARACTERS = 300
MAX_PUBLISHER_CHARACTERS = 200
MAX_URL_CHARACTERS = 2_048

# The whole retrieval, not one socket operation. A per-operation timeout is
# reset by every chunk that arrives, so a server sending one byte at a time
# can hold a request open indefinitely without ever tripping it.
TOTAL_DEADLINE_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 10.0

# Worst-case durable metadata per retrieval, used to prove the goal store
# cannot be bloated by a hostile page. Three URLs, a title and a publisher,
# plus room for field names and the retrieval timestamp.
MAX_DURABLE_METADATA_CHARACTERS = (
    3 * MAX_URL_CHARACTERS
    + MAX_TITLE_CHARACTERS
    + MAX_PUBLISHER_CHARACTERS
    + 512
)

# Search discovery bounds, D-025. Candidates are what AL/X reads to choose
# where to look next, so they are deliberately small: a snippet is a hint,
# not the page. Every field is provider-supplied text and therefore bounded
# for the same reason a page title is.
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 10
MAX_SNIPPET_CHARACTERS = 300
MAX_SUBJECT_CHARACTERS = 400
MAX_AGE_CHARACTERS = 64

# The most one search may put in front of the Core. Proved by test rather than
# assumed, because a provider that returned ten oversized candidates would
# otherwise cost more context than the page AL/X eventually reads.
MAX_SEARCH_PAYLOAD_CHARACTERS = MAX_SEARCH_RESULTS * (
    MAX_URL_CHARACTERS + MAX_TITLE_CHARACTERS + MAX_SNIPPET_CHARACTERS
    + MAX_AGE_CHARACTERS + 256
) + 512

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

    The parts are kept separately, never as one string to be edited later.
    Substituting an address into a URL by text is how `EXAMPLE.COM` survived
    into a transport destination and was resolved a second time: the spelling
    that was validated and the spelling that was replaced were not the same
    string. Both URLs below are therefore built from validated components.
    """

    scheme: str
    host: str
    port: int
    addresses: tuple[PublicAddress, ...]
    path: str = "/"
    query: str = ""

    def __post_init__(self) -> None:
        if self.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"scheme is not public web: {self.scheme}")
        if self.port not in ALLOWED_PORTS:
            raise ValueError(f"port is not a public web port: {self.port}")
        if not self.host.strip():
            raise ValueError("host must not be blank")
        if self.host != self.host.strip().lower().rstrip("."):
            raise ValueError("host must already be normalised")
        if not self.addresses:
            raise ValueError("a public URL requires at least one validated address")
        if not self.path.startswith("/"):
            raise ValueError("path must be absolute")

    @property
    def source_domain(self) -> str:
        return self.host

    def _authority(self, host: str) -> str:
        bracketed = f"[{host}]" if ":" in host else host
        default = 80 if self.scheme == "http" else 443
        return bracketed if self.port == default else f"{bracketed}:{self.port}"

    @property
    def url(self) -> str:
        """What AL/X asked for and what provenance records: the hostname URL.

        This is never the transport destination. A citation naming an IP
        address is not something a person can open, and it is not what she
        requested.
        """
        return f"{self.scheme}://{self._authority(self.host)}{self.path}{self.query}"

    def transport_url(self, address: "PublicAddress") -> str:
        """Where the socket actually goes: one validated literal address.

        The hostname does not appear here at all, so nothing downstream can
        resolve it a second time. `Host` and `sni_hostname` carry the name
        instead, which is what keeps the certificate check honest.
        """
        if address not in self.addresses:
            raise ValueError("transport address was not validated for this url")
        return (
            f"{self.scheme}://{self._authority(address.literal)}"
            f"{self.path}{self.query}"
        )

    @property
    def host_header(self) -> str:
        """The `Host` a well-behaved client would have sent for this URL."""
        return self._authority(self.host)


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
    # What the publisher says its own address is. Reported beside the fetched
    # URL, never in place of it: a page's claim about its identity is not the
    # same fact as where the page was actually read from.
    canonical_url: str | None = None

    def __post_init__(self) -> None:
        if not self.requested_url.strip() or not self.final_url.strip():
            raise ValueError("a retrieved page requires both URLs")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone aware")
        if self.content_omitted_characters < 0:
            raise ValueError("omitted characters cannot be negative")
        # Enforced on the record itself, so no construction path can return an
        # unbounded field. Every one of these is attacker-chosen text.
        for value, ceiling, name in (
            (self.content, MAX_EXTRACTED_CHARACTERS, "content"),
            (self.title, MAX_TITLE_CHARACTERS, "title"),
            (self.publisher, MAX_PUBLISHER_CHARACTERS, "publisher"),
            (self.requested_url, MAX_URL_CHARACTERS, "requested_url"),
            (self.final_url, MAX_URL_CHARACTERS, "final_url"),
            (self.canonical_url, MAX_URL_CHARACTERS, "canonical_url"),
            (self.source_domain, MAX_URL_CHARACTERS, "source_domain"),
        ):
            if value is not None and len(value) > ceiling:
                raise ValueError(
                    f"{name} exceeds its D-025 bound; it must be cut before "
                    "it reaches the Core or durable state, never returned whole"
                )


# Search refusals. Each names a distinct fact about the world, so AL/X can
# tell "nothing was found" from "I was not allowed to look" from "the day's
# allowance is spent". Code returns the fact and draws no conclusion from it.
SEARCH_UNAVAILABLE = "search_unavailable"
SEARCH_BUDGET_EXHAUSTED = "search_budget_exhausted"
SEARCH_TIMEOUT = "search_timeout"
SEARCH_PROVIDER_FAILED = "search_provider_failed"
SEARCH_AUTH_FAILED = "search_auth_failed"
SEARCH_RATE_LIMITED = "search_rate_limited"
SEARCH_NO_RESULTS = "search_no_results"

WEB_SEARCH_FAILURES = (
    SEARCH_UNAVAILABLE,
    SEARCH_BUDGET_EXHAUSTED,
    SEARCH_TIMEOUT,
    SEARCH_PROVIDER_FAILED,
    SEARCH_AUTH_FAILED,
    SEARCH_RATE_LIMITED,
    SEARCH_NO_RESULTS,
)


class WebSearchError(Exception):
    """A search refusal carrying one declared code and no provider content."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in WEB_SEARCH_FAILURES:
            raise ValueError(f"undeclared search failure code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(code)


def _bounded(value: str | None, ceiling: int, name: str) -> str | None:
    if value is None:
        return None
    if len(value) > ceiling:
        raise ValueError(
            f"{name} exceeds its D-025 bound; it must be cut before it reaches "
            "the Core or durable state, never returned whole"
        )
    return value


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """One candidate page the provider returned, in the order it returned it.

    Everything here is provider-supplied text about a page nobody has read
    yet. It carries no score, no rank of ours, no quality label and no
    preference: those would be judgements, and D-025 keeps them in the Core.
    """

    url: str
    title: str
    snippet: str
    source_domain: str
    # Whatever freshness the provider stated, verbatim. Recorded because when
    # a page is from matters to AL/X; not parsed, because interpreting it
    # would make code decide how current is current enough.
    age: str | None = None

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("a search result requires a url")
        _bounded(self.url, MAX_URL_CHARACTERS, "result url")
        _bounded(self.title, MAX_TITLE_CHARACTERS, "result title")
        _bounded(self.snippet, MAX_SNIPPET_CHARACTERS, "result snippet")
        _bounded(self.source_domain, MAX_URL_CHARACTERS, "result source_domain")
        _bounded(self.age, MAX_AGE_CHARACTERS, "result age")


@dataclass(frozen=True, slots=True)
class WebSearchResults:
    """One search, and the ordered candidates it produced.

    Deliberately carries no `search_id`. The identifier belongs to the call
    AL/X made, not to the provider's answer, and a provider forced to invent
    one would be naming something it knows nothing about. The capability pairs
    the two.
    """

    retrieved_at: datetime
    results: tuple[WebSearchResult, ...]

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone aware")
        if any(not isinstance(item, WebSearchResult) for item in self.results):
            raise TypeError("results must contain only WebSearchResult values")
        if len(self.results) > MAX_SEARCH_RESULTS:
            raise ValueError(
                f"a search may return at most {MAX_SEARCH_RESULTS} candidates"
            )

    @property
    def result_count(self) -> int:
        return len(self.results)


class WebSearchProvider(Protocol):
    """Runs exactly one search, or raises a declared refusal.

    `subject` is language AL/X composed. It is passed to the provider exactly
    as she wrote it: no expansion, no rewriting, no classification, no
    "improvement". Deterministic code that edited her wording would be
    deciding what she meant.
    """

    def search(self, subject: str, max_results: int) -> WebSearchResults: ...


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
