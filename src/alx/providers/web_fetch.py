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
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from alx.contracts.web import (
    ALLOWED_CONTENT_TYPES,
    MAX_DOWNLOAD_BYTES,
    MAX_EXTRACTED_CHARACTERS,
    MAX_REDIRECTS,
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
    """
    encoding = encoding.lower().strip()
    try:
        if encoding == "gzip":
            machine = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            machine = zlib.decompressobj()
        elif encoding in ("", "identity"):
            return raw
        else:
            raise WebRetrievalError(
                UNSUPPORTED_CONTENT_TYPE, f"content encoding: {encoding}"
            )
        out = machine.decompress(raw, MAX_DOWNLOAD_BYTES + 1)
        if len(out) > MAX_DOWNLOAD_BYTES:
            raise WebRetrievalError(PAGE_TOO_LARGE, "decompressed beyond the bound")
        return out
    except WebRetrievalError:
        raise
    except (zlib.error, gzip.BadGzipFile, OSError):
        raise WebRetrievalError(PROVIDER_FAILED, "malformed compression") from None


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
        connect_timeout: float = 10.0,
        total_timeout: float = 20.0,
        now: object = None,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(total_timeout, connect=connect_timeout),
            # Redirects are revalidated by hand; httpx must never follow one.
            follow_redirects=False,
            # No cookie jar: a public reader keeps no session across requests.
            cookies=None,
        )
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    def fetch(self, url: str, max_characters: int = MAX_EXTRACTED_CHARACTERS) -> WebPage:
        """Retrieve one page, following at most MAX_REDIRECTS validated hops."""
        bound = max(1, min(int(max_characters), MAX_EXTRACTED_CHARACTERS))
        requested = url
        current = url
        seen: set[str] = set()

        for _hop in range(MAX_REDIRECTS + 1):
            # Every hop is revalidated in full: scheme, credentials, port, and
            # every address the new hostname resolves to.
            public = parse_public_url(current, self._resolver)
            if public.url in seen:
                raise WebRetrievalError(RETRIEVAL_BLOCKED, "redirect loop")
            seen.add(public.url)

            try:
                with self._request(public) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise WebRetrievalError(
                                RETRIEVAL_BLOCKED, "redirect without location"
                            )
                        current = urljoin(public.url, location)
                        continue

                    return self._page(requested, public, response, bound)
            except httpx.TimeoutException:
                raise WebRetrievalError(
                    RETRIEVAL_TIMEOUT, "request timed out"
                ) from None
            except httpx.HTTPError as error:
                raise WebRetrievalError(
                    PROVIDER_FAILED, type(error).__name__
                ) from None

        raise WebRetrievalError(RETRIEVAL_BLOCKED, "too many redirects")

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
        authority = (
            f"[{address.literal}]" if ":" in address.literal else address.literal
        )
        pinned = public.url.replace(f"//{public.host}", f"//{authority}", 1)
        if f":{public.port}" not in pinned.split("/")[2]:
            pinned = pinned.replace(f"//{authority}", f"//{authority}:{public.port}", 1)

        host_header = (
            public.host
            if public.port in (80, 443)
            else f"{public.host}:{public.port}"
        )
        return self._client.stream(
            "GET",
            pinned,
            headers={
                "Host": host_header,
                "User-Agent": "AL/X-web-reader/1.0 (+public read-only)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            # Preserves certificate validation against the real hostname
            # even though the connection went to the literal address.
            extensions={"sni_hostname": public.host},
        )

    def _page(self, requested: str, public, response: httpx.Response, bound: int) -> WebPage:
        status = response.status_code
        if status in (401, 403, 429):
            raise WebRetrievalError(RETRIEVAL_BLOCKED, f"status {status}")
        if status >= 400:
            raise WebRetrievalError(PROVIDER_FAILED, f"status {status}")

        content_type = response.headers.get("content-type", "")
        media = content_type.split(";")[0].strip().lower()
        if media and media not in ALLOWED_CONTENT_TYPES:
            raise WebRetrievalError(UNSUPPORTED_CONTENT_TYPE, media)

        raw = self._read_bounded(response)
        body = _decompress(raw, response.headers.get("content-encoding", ""))
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
        final = str(response.url)
        # A canonical link is the publisher's own statement of the page's
        # address. It is recorded as the final URL only when it names the same
        # host, so a canonical tag cannot move provenance to another site.
        if canonical:
            resolved = urljoin(public.url, canonical)
            try:
                if parse_public_url(resolved, self._resolver).host == public.host:
                    final = resolved
            except WebRetrievalError:
                pass

        return WebPage(
            requested_url=requested,
            final_url=final,
            source_domain=public.source_domain,
            retrieved_at=self._now(),
            http_status=status,
            content=text[:bound],
            content_omitted_characters=omitted,
            title=title or None,
            publisher=publisher or None,
        )

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
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
