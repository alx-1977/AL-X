"""Bounded retrieval of one public web page, under D-025.

The request is pinned to an address `web_url` already validated, while TLS and
the `Host` header keep using the original hostname. That combination is what
makes the boundary hold: the socket cannot be moved to another destination by
a second DNS answer, and the certificate is still checked against the name
AL/X asked for. httpcore reads `sni_hostname` from the request extensions
independently of the connection origin, which is what allows both at once
without a custom transport.

Redirects are followed here rather than by httpx, because every hop has to go
back through the whole boundary. An automatically followed redirect would
inherit the pinned address and the previous hostname's certificate check, so
one public URL could walk the connection somewhere private.

Extraction is stdlib only and deliberately modest. It recovers a title, a
publisher and readable text; it is not a renderer and does not run scripts. A
page that turns out to be an empty shell around JavaScript is reported as
`unsupported_dynamic_page` rather than returned as though it were blank.
"""

from __future__ import annotations

import gzip
import re
import zlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from time import monotonic as monotonic_clock
from urllib.parse import urljoin, urlsplit

import httpx

from alx.contracts.web import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_PORTS,
    ALLOWED_SCHEMES,
    CONNECT_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_BYTES,
    MAX_EXTRACTED_CHARACTERS,
    MAX_PUBLISHER_CHARACTERS,
    MAX_REDIRECTS,
    MAX_TITLE_CHARACTERS,
    MAX_URL_CHARACTERS,
    TOTAL_DEADLINE_SECONDS,
    PAGE_TOO_LARGE,
    PROVIDER_FAILED,
    RETRIEVAL_BLOCKED,
    RETRIEVAL_TIMEOUT,
    UNSUPPORTED_CONTENT_TYPE,
    UNSUPPORTED_DYNAMIC_PAGE,
    WebPage,
    WebRetrievalError,
)
from alx.providers.web_url import Resolver, parse_public_url


# A page whose markup is mostly script and which yields almost no text was not
# blank; it was never rendered. Reporting that honestly is more useful to AL/X
# than handing her an empty string, and it is a mechanical observation about
# bytes rather than a judgement about content.
DYNAMIC_TEXT_FLOOR = 200
DYNAMIC_SCRIPT_FLOOR = 1_000

# Elements whose text is furniture rather than content. Dropping them is a
# mechanical structural decision, not a judgement about what a page is saying.
_DROPPED = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas",
     "nav", "footer", "aside", "form", "head"}
)
_BLOCK = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
     "section", "article", "header", "blockquote", "pre", "td", "table"}
)

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _Extractor(HTMLParser):
    """Collect a title, metadata and readable text from one document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.publisher: str | None = None
        self.canonical: str | None = None
        self.script_characters = 0
        self._parts: list[str] = []
        self._dropped_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROPPED:
            self._dropped_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._meta(dict(attrs))
        elif tag == "link":
            values = dict(attrs)
            if (values.get("rel") or "").lower() == "canonical":
                self.canonical = values.get("href") or self.canonical
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "meta":
            self._meta(dict(attrs))
        elif tag == "link":
            values = dict(attrs)
            if (values.get("rel") or "").lower() == "canonical":
                self.canonical = values.get("href") or self.canonical
        elif tag == "br":
            self._parts.append("\n")

    def _meta(self, values: dict) -> None:
        name = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content") or ""
        if not content:
            return
        if name in ("og:site_name", "application-name") and not self.publisher:
            self.publisher = content.strip()
        elif name == "og:title" and not self.title:
            self.title = content.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropped_depth = max(0, self._dropped_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data
            return
        if self._dropped_depth:
            # Counted, not kept: the volume of script is how a shell page is
            # recognised, but its contents are never treated as page text.
            self.script_characters += len(data)
            return
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        joined = _WHITESPACE.sub(" ", joined)
        lines = [line.strip() for line in joined.split("\n")]
        return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def extract(markup: str) -> tuple[str, str | None, str | None, str | None, int]:
    """Return (text, title, publisher, canonical, script_characters)."""
    parser = _Extractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        # Malformed markup is ordinary on the web. Whatever was parsed before
        # the fault is still real text, so the parse stops rather than failing.
        pass
    title = parser.title.strip() if parser.title else None
    return (
        parser.text(),
        _WHITESPACE.sub(" ", title) if title else None,
        parser.publisher,
        parser.canonical,
        parser.script_characters,
    )


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decompress within the same ceiling that bounded the download.

    A compressed response that expands past the ceiling is refused. Trusting
    the compressed size would let a small download become an unbounded buffer.

    Every concatenated member is consumed, not just the first. A gzip stream
    may hold several members, and returning only the first would hand back
    part of a page while reporting a complete read — evidence that is wrong
    without saying so. The aggregate output is held to the same ceiling, and
    input that cannot be consumed is a failure rather than a silent truncation.
    """
    encoding = encoding.lower().strip()
    if encoding in ("", "identity"):
        return raw
    if encoding not in ("gzip", "deflate", "x-gzip"):
        raise WebRetrievalError(
            UNSUPPORTED_CONTENT_TYPE, f"content encoding: {encoding}"
        )
    window = 16 + zlib.MAX_WBITS if encoding in ("gzip", "x-gzip") else zlib.MAX_WBITS
    remaining = raw
    chunks: list[bytes] = []
    total = 0
    try:
        while remaining:
            machine = zlib.decompressobj(window)
            produced = machine.decompress(remaining, MAX_DOWNLOAD_BYTES + 1 - total)
            total += len(produced)
            if total > MAX_DOWNLOAD_BYTES:
                raise WebRetrievalError(
                    PAGE_TOO_LARGE, "decompressed beyond the bound"
                )
            chunks.append(produced)
            if machine.unconsumed_tail:
                # Output was cut short by the ceiling rather than by the end of
                # the stream, so the remaining input cannot fit either.
                raise WebRetrievalError(
                    PAGE_TOO_LARGE, "decompressed beyond the bound"
                )
            if not machine.eof:
                # A member that never ended means the body was truncated in
                # transit. Returning what arrived would be partial evidence.
                raise WebRetrievalError(PROVIDER_FAILED, "incomplete compressed body")
            leftover = machine.unused_data
            if leftover == remaining:
                raise WebRetrievalError(PROVIDER_FAILED, "compressed body stalled")
            remaining = leftover
    except WebRetrievalError:
        raise
    except (zlib.error, gzip.BadGzipFile, OSError):
        raise WebRetrievalError(PROVIDER_FAILED, "malformed compression") from None
    return b"".join(chunks)


def _charset(content_type: str, body: bytes) -> str:
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"\'')
    found = re.search(
        rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", body[:4096], re.I
    )
    return found.group(1).decode("ascii", "ignore") if found else "utf-8"


class HttpWebFetchProvider:
    """The one production path from a URL to a bounded, provenanced page."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        total_deadline: float = TOTAL_DEADLINE_SECONDS,
        now: object = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            # Per-operation bounds only. They stop one stalled socket; they do
            # not bound the retrieval, because every arriving chunk resets the
            # read timer. The wall-clock deadline below is what actually ends
            # a slow-drip response.
            timeout=httpx.Timeout(total_deadline, connect=connect_timeout),
            # Redirects are revalidated by hand; httpx must never follow one.
            follow_redirects=False,
            # No cookie jar: a public reader keeps no session across requests.
            cookies=None,
        )
        self._resolver = resolver
        self._total_deadline = total_deadline
        self._now = now or (lambda: datetime.now(UTC))
        # Injected so a deadline can be proved with a controlled clock rather
        # than by sleeping, which would make the test slow and flaky.
        self._monotonic = monotonic or monotonic_clock

    def fetch(self, url: str, max_characters: int = MAX_EXTRACTED_CHARACTERS) -> WebPage:
        """Retrieve one page, following at most MAX_REDIRECTS validated hops.

        One deadline covers the whole operation — resolution, connection,
        headers, every redirect, body streaming and decompression. It is taken
        once here and never extended, so a redirect chain cannot buy more time
        than a single request would have had.
        """
        bound = max(1, min(int(max_characters), MAX_EXTRACTED_CHARACTERS))
        requested = url
        current = url
        seen: set[str] = set()
        expires_at = self._monotonic() + self._total_deadline

        for _hop in range(MAX_REDIRECTS + 1):
            self._check_deadline(expires_at)
            # Every hop is revalidated in full: scheme, credentials, port, and
            # every address the new hostname resolves to. Nothing from the
            # previous hop survives — the address, Host and SNI are all derived
            # from this newly validated URL.
            public = parse_public_url(current, self._resolver)
            if public.url in seen:
                raise WebRetrievalError(RETRIEVAL_BLOCKED, "redirect loop")
            seen.add(public.url)

            try:
                with self._request(public) as response:
                    self._check_deadline(expires_at)
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise WebRetrievalError(
                                RETRIEVAL_BLOCKED, "redirect without location"
                            )
                        # Resolved against the validated hostname URL, never
                        # against the pinned transport URL, so a relative
                        # Location cannot inherit the literal address.
                        current = urljoin(public.url, location)
                        continue

                    return self._page(requested, public, response, bound, expires_at)
            except httpx.TimeoutException:
                raise WebRetrievalError(
                    RETRIEVAL_TIMEOUT, "request timed out"
                ) from None
            except httpx.HTTPError as error:
                raise WebRetrievalError(
                    PROVIDER_FAILED, type(error).__name__
                ) from None

        raise WebRetrievalError(RETRIEVAL_BLOCKED, "too many redirects")

    def _check_deadline(self, expires_at: float) -> None:
        if self._monotonic() >= expires_at:
            raise WebRetrievalError(RETRIEVAL_TIMEOUT, "total deadline expired")

    def _request(self, public):
        """Connect to a validated address while keeping the real hostname.

        The URL carries the literal address, so httpcore connects there and
        nothing re-resolves the name. `sni_hostname` and `Host` carry the
        original hostname, so TLS validates the certificate against the site
        AL/X asked for. Dropping either half would silently disable one of the
        two protections.

        The response is streamed rather than read whole. `Client.request` would
        buffer the entire body before returning, so the byte ceiling below
        would be counting bytes that were already in memory — a measurement
        rather than a bound.
        """
        address = public.addresses[0]
        return self._client.stream(
            "GET",
            # Built from validated components, never by editing the original
            # URL. A text substitution missed `EXAMPLE.COM`, leaving the
            # hostname in the destination for httpx to resolve a second time.
            public.transport_url(address),
            headers={
                "Host": public.host_header,
                "User-Agent": "AL/X-web-reader/1.0 (+public read-only)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            # Preserves certificate validation against the real hostname
            # even though the connection went to the literal address.
            extensions={"sni_hostname": public.host},
        )

    def _page(
        self,
        requested: str,
        public,
        response: httpx.Response,
        bound: int,
        expires_at: float,
    ) -> WebPage:
        status = response.status_code
        if status in (401, 403, 429):
            raise WebRetrievalError(RETRIEVAL_BLOCKED, f"status {status}")
        if status >= 400:
            raise WebRetrievalError(PROVIDER_FAILED, f"status {status}")

        content_type = response.headers.get("content-type", "")
        media = content_type.split(";")[0].strip().lower()
        # Fails closed. D-025 authorises only the declared textual types, and
        # an absent or unparseable type is not one of them. Bytes that happen
        # to decode are not evidence that a response was ever text.
        if media not in ALLOWED_CONTENT_TYPES:
            raise WebRetrievalError(
                UNSUPPORTED_CONTENT_TYPE, media or "missing content type"
            )

        raw = self._read_bounded(response, expires_at)
        body = _decompress(raw, response.headers.get("content-encoding", ""))
        self._check_deadline(expires_at)
        markup = body.decode(_charset(content_type, body), errors="replace")

        if media == "text/plain":
            text, title, publisher, canonical, scripts = (
                markup.strip(), None, None, None, 0
            )
        else:
            text, title, publisher, canonical, scripts = extract(markup)

        if len(text) < DYNAMIC_TEXT_FLOOR and scripts >= DYNAMIC_SCRIPT_FLOOR:
            raise WebRetrievalError(
                UNSUPPORTED_DYNAMIC_PAGE, "page requires script execution"
            )

        omitted = max(0, len(text) - bound)
        # Provenance is the validated hostname URL of the hop actually fetched
        # — never the pinned transport URL, which names an IP address nobody
        # can open, and never the page's own claim about itself.
        final = public.url
        canonical_url = self._canonical(public, canonical, expires_at)

        return WebPage(
            requested_url=requested,
            final_url=final,
            source_domain=public.source_domain,
            retrieved_at=self._now(),
            http_status=status,
            content=text[:bound],
            content_omitted_characters=omitted,
            # Every page-controlled field is cut to its own ceiling. A title is
            # attacker-chosen text just as the body is, and it is the half that
            # reaches durable state.
            title=(title.strip()[:MAX_TITLE_CHARACTERS] or None) if title else None,
            publisher=(
                (publisher.strip()[:MAX_PUBLISHER_CHARACTERS] or None)
                if publisher
                else None
            ),
            canonical_url=canonical_url,
        )

    def _canonical(
        self, public, declared: str | None, expires_at: float
    ) -> str | None:
        """The publisher's claim about its own address, reported separately.

        It is never allowed to become `final_url`. A page asserting where it
        lives is not the same fact as where it was read from, and letting the
        assertion win would let a page rewrite its own provenance. It is kept
        only when it names the same host, so it cannot even point the reader
        at another site.

        Validated without resolving anything. The host is compared textually
        against the hostname already validated for this hop, so a page cannot
        spend the caller's remaining time on a DNS lookup for a name of its
        choosing — the deadline is checked, and a canonical link is metadata
        rather than a destination this fetch will ever connect to.

        What is kept is normalised and held to the same scheme, port and
        credential rules as any other URL. A citation recorded in a spelling
        this boundary would itself refuse — port 8080, say — would be a
        durable pointer at something AL/X could never open.
        """
        if not declared or not declared.strip():
            return None
        if len(declared) > MAX_URL_CHARACTERS:
            return None
        self._check_deadline(expires_at)
        try:
            # Checked before joining. `urljoin` treats anything it cannot
            # parse as a relative path, so `ht!tp://[[[/x` would silently
            # become `http://example.com/ht!tp:/[[[/x` — a durable citation
            # to a page that never existed. A canonical is either a usable
            # URL or it is omitted; it is never repaired into a guess.
            stated = urlsplit(declared)
            if stated.scheme and stated.scheme.lower() not in ALLOWED_SCHEMES:
                return None
            if stated.scheme and not stated.netloc:
                return None
            if not stated.scheme and not stated.netloc:
                if not stated.path:
                    # Only a fragment or query: it names no page of its own,
                    # and `PublicUrl` carries no fragment, so recording it
                    # would drop the part the publisher actually stated.
                    return None
                # A relative canonical is a path. Anything holding a colon
                # before its first slash, or a backslash, is a mangled
                # absolute URL that `urljoin` would graft onto this host —
                # `ht!tp://[[[/x` becoming `http://example.com/ht!tp:/[[[/x`,
                # a citation to a page that never existed.
                head = stated.path.split("/", 1)[0]
                if ":" in head or "\\" in stated.path:
                    return None
            resolved = urljoin(public.url, declared)
            if len(resolved) > MAX_URL_CHARACTERS:
                return None
            parts = urlsplit(resolved)
            scheme = parts.scheme.lower()
            if scheme not in ALLOWED_SCHEMES:
                return None
            if parts.fragment:
                # Nothing here can carry it, so a citation would silently
                # differ from what the page declared.
                return None
            if "@" in parts.netloc:
                return None
            host = (parts.hostname or "").strip().rstrip(".").lower()
            port = parts.port
        except ValueError:
            return None
        if host != public.host:
            return None
        if port is not None and port not in ALLOWED_PORTS:
            return None
        # Rebuilt from the checked parts rather than echoed back, so the
        # recorded citation is the same shape as every other URL here — and
        # rebuilt by the same code, because a second copy of the authority
        # rules is how this one lost its IPv6 brackets and recorded a
        # citation that could never be opened again.
        return replace(
            public,
            scheme=scheme,
            # A canonical may legitimately name https where the hop was http.
            # Its own port wins when it gave one; otherwise the default for
            # its own scheme, not the port this hop happened to use.
            port=port or (80 if scheme == "http" else 443),
            path=parts.path or "/",
            query=f"?{parts.query}" if parts.query else "",
        ).url

    def _read_bounded(self, response: httpx.Response, expires_at: float) -> bytes:
        """Count the bytes actually on the wire; never trust Content-Length.

        A declared length is a claim by the server, so the arriving bytes are
        counted instead. Deliberately `iter_raw`: `iter_bytes` decompresses
        inside httpx and can return a whole inflated body as one chunk, so a
        compression bomb would already be in memory before any check ran.
        Reading the compressed stream keeps the expansion under `_decompress`,
        which stops at the same ceiling.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_raw():
            # Checked per chunk, because a per-operation read timeout is reset
            # by every byte that arrives. A server drip-feeding one byte before
            # each timeout would otherwise hold this request open forever.
            self._check_deadline(expires_at)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise WebRetrievalError(PAGE_TOO_LARGE, "download exceeded the bound")
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._client.close()


__all__ = [
    "DYNAMIC_SCRIPT_FLOOR",
    "DYNAMIC_TEXT_FLOOR",
    "HttpWebFetchProvider",
    "extract",
]
