"""End-to-end facts about a real retrieval, not the correctness of its parts.

Every defect the first Greptile review found was invisible to a green suite,
because the suite tested normalised helper outputs while the unsafe behaviour
only appeared in composition. `PublicUrl.host` was correctly lowercased *and*
the hostname still reached the transport; `final_url` was correctly returned
*and* it named an IP address, because the test's stub supplied that field
itself.

So these tests assert what an observer outside the provider would see: the
exact URL handed to the transport, the exact Host and SNI, and the exact
provenance that comes back. The real `HttpWebFetchProvider` is exercised
throughout — a stub that manufactures `final_url` proves nothing about the
code that builds it.
"""

from __future__ import annotations

import gzip
import socket
import sys
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from alx.contracts.web import (
    MAX_DURABLE_METADATA_CHARACTERS,
    MAX_PUBLISHER_CHARACTERS,
    MAX_TITLE_CHARACTERS,
    MAX_URL_CHARACTERS,
    PAGE_TOO_LARGE,
    PROVIDER_FAILED,
    RETRIEVAL_TIMEOUT,
    UNSUPPORTED_CONTENT_TYPE,
    URL_NOT_PUBLIC,
    WebRetrievalError,
)
from alx.providers.web_fetch import HttpWebFetchProvider
from alx.providers.web_url import parse_public_url
from alx.tools import ASK_WEB_PAGE
from alx.tools.web import build_web_executors


PUBLIC_IP = "93.184.216.34"
OTHER_IP = "198.41.0.4"
ROUTES: dict = {}


class FixtureServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Quiet: an aborted oversized download is the behaviour under test."""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        route = ROUTES.get(self.path.split("?")[0])
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, headers, body = route(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Length" not in headers and "omit_length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def page(html: str, content_type: str = "text/html; charset=utf-8"):
    return lambda h: (200, {"Content-Type": content_type}, html.encode())


def redirect(target: str, status: int = 302):
    return lambda h: (status, {"Location": target, "Content-Length": "0"}, b"")


class RecordingTransport(httpx.BaseTransport):
    """Captures exactly what the provider handed the network, then serves it.

    This is the observation point the old suite lacked. Assertions run against
    `sent`, so a hostname that survives into the destination is visible here
    even when every helper reported a normalised value.
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx.HTTPTransport()
        self.sent: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        moved = request.url.copy_with(host="127.0.0.1", port=self._port, scheme="http")
        return self._inner.handle_request(
            httpx.Request(
                request.method, moved,
                headers=request.headers, extensions=request.extensions,
            )
        )


def resolving(mapping: dict[str, str], default: str = PUBLIC_IP):
    def resolve(host: str, port: int):
        literal = mapping.get(host, default)
        family = socket.AF_INET6 if ":" in literal else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (literal, port))]

    return resolve


class ComposedTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FixtureServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        ROUTES.clear()
        self.transport = RecordingTransport(self.port)
        self.client = httpx.Client(
            timeout=5.0, follow_redirects=False, transport=self.transport
        )
        self.addCleanup(self.client.close)

    def provider(self, resolver=None, **kwargs) -> HttpWebFetchProvider:
        return HttpWebFetchProvider(
            client=self.client,
            resolver=resolver or resolving({}),
            **kwargs,
        )

    @property
    def last(self) -> httpx.Request:
        return self.transport.sent[-1]


class TransportDestinationTests(ComposedTestCase):
    """A: input URL -> DNS answer -> exact transport URL, Host, SNI, final_url."""

    def test_the_hostname_never_reaches_the_transport(self) -> None:
        ROUTES["/article"] = page("<html><body><p>text here</p></body></html>")
        result = self.provider().fetch("http://example.com/article")

        self.assertEqual(str(self.last.url), f"http://{PUBLIC_IP}/article")
        self.assertEqual(self.last.headers["Host"], "example.com")
        self.assertEqual(self.last.extensions["sni_hostname"], "example.com")
        self.assertEqual(result.final_url, "http://example.com/article")

    def test_host_spellings_cannot_defeat_pinning(self) -> None:
        """D: uppercase and trailing dots must not leave a name to resolve."""
        ROUTES["/x"] = page("<html><body><p>text here</p></body></html>")
        for raw in (
            "http://EXAMPLE.COM/x",
            "http://example.com./x",
            "http://EXAMPLE.com./x",
            "http://ExAmPlE.CoM./x",
        ):
            with self.subTest(raw=raw):
                result = self.provider().fetch(raw)
                sent = str(self.last.url)
                self.assertEqual(sent, f"http://{PUBLIC_IP}/x")
                self.assertNotIn("example", sent.lower())
                self.assertEqual(self.last.headers["Host"], "example.com")
                self.assertEqual(
                    self.last.extensions["sni_hostname"], "example.com"
                )
                self.assertEqual(result.final_url, "http://example.com/x")

    def test_a_query_string_survives_pinning_unchanged(self) -> None:
        ROUTES["/search"] = page("<html><body><p>text here</p></body></html>")
        result = self.provider().fetch("http://example.com/search?q=1&r=two")
        self.assertEqual(str(self.last.url), f"http://{PUBLIC_IP}/search?q=1&r=two")
        self.assertEqual(result.final_url, "http://example.com/search?q=1&r=two")

    def test_percent_encoding_survives_pinning_unchanged(self) -> None:
        ROUTES["/a%20b"] = page("<html><body><p>text here</p></body></html>")
        result = self.provider().fetch("http://example.com/a%20b")
        self.assertEqual(str(self.last.url), f"http://{PUBLIC_IP}/a%20b")
        self.assertEqual(result.final_url, "http://example.com/a%20b")

    def test_an_ipv6_answer_is_bracketed_in_the_destination(self) -> None:
        ROUTES["/x"] = page("<html><body><p>text here</p></body></html>")
        literal = "2606:2800:220:1:248:1893:25c8:1946"
        self.provider(resolver=resolving({}, literal)).fetch("http://example.com/x")
        self.assertEqual(str(self.last.url), f"http://[{literal}]/x")
        self.assertEqual(self.last.headers["Host"], "example.com")


class RedirectCompositionTests(ComposedTestCase):
    """B and C: each hop revalidates, and no state survives it."""

    def test_each_hop_gets_its_own_address_host_and_sni(self) -> None:
        ROUTES["/from"] = redirect("http://other.example/to")
        ROUTES["/to"] = page("<html><body><p>arrived here now</p></body></html>")
        resolver = resolving({"example.com": PUBLIC_IP, "other.example": OTHER_IP})

        result = self.provider(resolver=resolver).fetch("http://example.com/from")

        first, second = self.transport.sent
        self.assertEqual(str(first.url), f"http://{PUBLIC_IP}/from")
        self.assertEqual(first.headers["Host"], "example.com")
        self.assertEqual(first.extensions["sni_hostname"], "example.com")

        self.assertEqual(str(second.url), f"http://{OTHER_IP}/to")
        self.assertEqual(second.headers["Host"], "other.example")
        self.assertEqual(second.extensions["sni_hostname"], "other.example")

        # Provenance is the hostname of the hop actually fetched last.
        self.assertEqual(result.requested_url, "http://example.com/from")
        self.assertEqual(result.final_url, "http://other.example/to")
        self.assertEqual(result.source_domain, "other.example")

    def test_a_relative_redirect_resolves_against_the_hostname_url(self) -> None:
        """Never against the pinned URL, or the IP would become the base."""
        ROUTES["/from"] = redirect("/landing")
        ROUTES["/landing"] = page("<html><body><p>arrived here now</p></body></html>")
        result = self.provider().fetch("http://example.com/from")
        self.assertEqual(str(self.last.url), f"http://{PUBLIC_IP}/landing")
        self.assertEqual(result.final_url, "http://example.com/landing")

    def test_a_redirect_toward_a_private_address_is_never_requested(self) -> None:
        """C: refusal happens before a second request is issued."""
        ROUTES["/evil"] = redirect("http://internal.example/secrets")
        resolver = resolving(
            {"example.com": PUBLIC_IP, "internal.example": "10.0.0.1"}
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider(resolver=resolver).fetch("http://example.com/evil")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)
        self.assertEqual(len(self.transport.sent), 1)

    def test_a_redirect_to_shared_address_space_is_refused(self) -> None:
        ROUTES["/cgnat"] = redirect("http://carrier.example/x")
        resolver = resolving({"example.com": PUBLIC_IP, "carrier.example": "100.64.0.1"})
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider(resolver=resolver).fetch("http://example.com/cgnat")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)
        self.assertEqual(len(self.transport.sent), 1)


class ProvenanceCompositionTests(ComposedTestCase):
    """E and F: provenance is what was fetched, not what the page claims."""

    def test_a_page_without_a_canonical_tag_still_cites_its_hostname(self) -> None:
        """E: the ordinary path. This is what recorded an IP address before."""
        ROUTES["/plain"] = page("<html><body><p>ordinary article text</p></body></html>")
        result = self.provider().fetch("http://example.com/plain")
        self.assertEqual(result.final_url, "http://example.com/plain")
        self.assertNotIn(PUBLIC_IP, result.final_url)
        self.assertIsNone(result.canonical_url)

    def test_a_canonical_tag_cannot_rewrite_the_fetched_url(self) -> None:
        """F: a publisher's assertion is not a retrieval fact."""
        ROUTES["/real"] = page(
            "<html><head><link rel='canonical' href='http://example.com/preferred'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/real")
        self.assertEqual(result.final_url, "http://example.com/real")
        self.assertEqual(result.canonical_url, "http://example.com/preferred")

    def test_a_cross_site_canonical_is_discarded_entirely(self) -> None:
        ROUTES["/hijack"] = page(
            "<html><head><link rel='canonical' href='http://evil.example/owned'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/hijack")
        self.assertEqual(result.final_url, "http://example.com/hijack")
        self.assertIsNone(result.canonical_url)

    def test_a_canonical_link_never_resolves_a_hostname(self) -> None:
        """A page must not be able to spend the caller's time on a lookup.

        The canonical host is compared textually against the hostname already
        validated for this hop. Resolving it would let a page choose a name
        for this process to look up, after the last deadline check.
        """
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' href='http://slow.example/x'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        lookups = []

        def counting(host: str, port: int):
            lookups.append(host)
            return resolving({})(host, port)

        result = self.provider(resolver=counting).fetch("http://example.com/c")
        self.assertEqual(lookups, ["example.com"])
        self.assertIsNone(result.canonical_url)

    def test_a_canonical_link_is_refused_once_the_deadline_has_passed(self) -> None:
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' href='http://example.com/pref'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        # Every earlier checkpoint is inside the budget; time runs out only
        # at the canonical check, which is the one under test here.
        ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0] + [999.0] * 200)
        provider = HttpWebFetchProvider(
            client=self.client, resolver=resolving({}),
            total_deadline=20.0, monotonic=lambda: next(ticks),
        )
        with self.assertRaises(WebRetrievalError) as caught:
            provider.fetch("http://example.com/c")
        self.assertEqual(caught.exception.code, RETRIEVAL_TIMEOUT)

    def test_a_recorded_canonical_is_normalised_and_refetchable(self) -> None:
        """A citation must name something AL/X could actually open."""
        cases = {
            "http://EXAMPLE.COM/x": "http://example.com/x",
            "http://example.com./x": "http://example.com/x",
            "http://example.com:80/x": "http://example.com/x",
            "/relative": "http://example.com/relative",
        }
        for href, expected in cases.items():
            with self.subTest(href=href):
                ROUTES["/c"] = page(
                    f"<html><head><link rel='canonical' href='{href}'></head>"
                    "<body><p>ordinary article text</p></body></html>"
                )
                result = self.provider().fetch("http://example.com/c")
                self.assertEqual(result.canonical_url, expected)
                # And it survives the boundary if she ever asks to read it.
                parse_public_url(result.canonical_url, resolving({}))

    def test_a_canonical_keeps_its_own_scheme(self) -> None:
        """An http page may legitimately declare an https canonical."""
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' href='https://example.com/secure'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/c")
        self.assertEqual(result.canonical_url, "https://example.com/secure")
        self.assertEqual(result.final_url, "http://example.com/c")

    def test_an_ipv6_canonical_keeps_its_brackets(self) -> None:
        """Rebuilding an authority by hand is how brackets get lost."""
        literal = "2606:2800:220:1:248:1893:25c8:1946"
        resolver = resolving({}, literal)
        ROUTES["/c"] = page(
            f"<html><head><link rel='canonical' href='https://[{literal}]/pref'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider(resolver=resolver).fetch(f"https://[{literal}]/c")
        self.assertEqual(result.canonical_url, f"https://[{literal}]/pref")
        # A citation that cannot be reopened is not provenance.
        parse_public_url(result.canonical_url, resolver)

    def test_a_relative_canonical_on_ipv6_keeps_its_brackets(self) -> None:
        literal = "2606:2800:220:1:248:1893:25c8:1946"
        resolver = resolving({}, literal)
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' href='/rel'></head>"
            "<body><p>ordinary article text</p></body></html>"
        )
        result = self.provider(resolver=resolver).fetch(f"https://[{literal}]/c")
        self.assertEqual(result.canonical_url, f"https://[{literal}]/rel")
        parse_public_url(result.canonical_url, resolver)

    def test_a_canonical_on_a_forbidden_port_is_discarded(self) -> None:
        """Recording it would durably cite a URL the boundary would refuse."""
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' href='http://example.com:8080/x'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        self.assertIsNone(self.provider().fetch("http://example.com/c").canonical_url)

    def test_a_credentialed_canonical_is_discarded(self) -> None:
        ROUTES["/c"] = page(
            "<html><head><link rel='canonical' "
            "href='http://example.com@evil.example/x'></head>"
            "<body><p>ordinary article text</p></body></html>"
        )
        self.assertIsNone(self.provider().fetch("http://example.com/c").canonical_url)

    def test_a_private_canonical_is_discarded(self) -> None:
        ROUTES["/p"] = page(
            "<html><head><link rel='canonical' href='http://127.0.0.1/admin'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/p")
        self.assertIsNone(result.canonical_url)
        self.assertEqual(result.final_url, "http://example.com/p")


class OverlongUrlTests(ComposedTestCase):
    """A URL too long to record is refused, never fetched and mis-cited."""

    def test_an_overlong_url_is_refused_before_any_request(self) -> None:
        long_url = "http://example.com/" + "b" * (MAX_URL_CHARACTERS + 100)
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch(long_url)
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)
        self.assertEqual(self.transport.sent, [])

    def test_an_overlong_redirect_target_is_refused(self) -> None:
        ROUTES["/go"] = redirect("http://example.com/" + "b" * (MAX_URL_CHARACTERS + 100))
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch("http://example.com/go")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)
        self.assertEqual(len(self.transport.sent), 1)

    def test_a_recorded_url_is_never_a_shortened_one(self) -> None:
        """What is cited must be exactly what was fetched."""
        path = "/x" + "y" * 1500
        ROUTES[path] = page("<html><body><p>ordinary article text</p></body></html>")
        result = self.provider().fetch(f"http://example.com{path}")
        self.assertEqual(result.requested_url, f"http://example.com{path}")
        self.assertEqual(result.final_url, f"http://example.com{path}")


class MetadataBoundTests(ComposedTestCase):
    """G: every page-controlled field is bounded, not only the body."""

    def test_an_enormous_title_is_cut(self) -> None:
        ROUTES["/t"] = page(
            f"<html><head><title>{'T' * 400_000}</title></head>"
            "<body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/t")
        self.assertLessEqual(len(result.title), MAX_TITLE_CHARACTERS)

    def test_an_enormous_publisher_is_cut(self) -> None:
        ROUTES["/p"] = page(
            f"<html><head><meta property='og:site_name' content='{'P' * 400_000}'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/p")
        self.assertLessEqual(len(result.publisher), MAX_PUBLISHER_CHARACTERS)

    def test_an_enormous_canonical_is_discarded(self) -> None:
        ROUTES["/c"] = page(
            f"<html><head><link rel='canonical' href='http://example.com/{'c' * 400_000}'>"
            "</head><body><p>ordinary article text</p></body></html>"
        )
        result = self.provider().fetch("http://example.com/c")
        self.assertTrue(
            result.canonical_url is None
            or len(result.canonical_url) <= MAX_URL_CHARACTERS
        )

    def test_durable_state_has_a_bounded_worst_case(self) -> None:
        """A hostile page cannot bloat the goal store through metadata."""
        ROUTES["/all"] = page(
            f"<html><head><title>{'T' * 400_000}</title>"
            f"<meta property='og:site_name' content='{'P' * 400_000}'>"
            f"<link rel='canonical' href='http://example.com/{'c' * 400_000}'>"
            f"</head><body><p>{'body ' * 100_000}</p></body></html>"
        )
        executors = build_web_executors(self.provider(), lambda: "call-1")
        result = executors[ASK_WEB_PAGE](
            {"page_id": "p1", "url": "http://example.com/all"}
        )
        durable = "".join(f"{k}{v}" for k, v in result.durable_values.items())
        self.assertLessEqual(len(durable), MAX_DURABLE_METADATA_CHARACTERS)
        self.assertNotIn("content", result.durable_values)

    def test_the_core_payload_stays_bounded_for_a_hostile_page(self) -> None:
        ROUTES["/all"] = page(
            f"<html><head><title>{'T' * 400_000}</title>"
            f"<meta property='og:site_name' content='{'P' * 400_000}'></head>"
            f"<body><p>{'body ' * 100_000}</p></body></html>"
        )
        executors = build_web_executors(self.provider(), lambda: "call-1")
        result = executors[ASK_WEB_PAGE](
            {"page_id": "p1", "url": "http://example.com/all"}
        )
        payload = "".join(f"{k}{v}" for k, v in result.values.items())
        # Body bound plus every metadata bound, with room for field names.
        self.assertLess(len(payload), 8_000 + MAX_DURABLE_METADATA_CHARACTERS)


class DeadlineTests(ComposedTestCase):
    """H: a wall-clock deadline, proved with a controlled clock."""

    def test_a_slow_drip_body_hits_the_deadline_while_streaming(self) -> None:
        """The clock only passes the deadline once the body is arriving.

        Every earlier checkpoint is inside the budget, so this can only be
        stopped by the check inside the streaming loop. A version that trusted
        httpx's per-operation read timeout would read the whole body, because
        that timer is reset by each chunk that arrives.
        """
        body = b"<html><body><p>" + b"drip " * 4000 + b"</p></body></html>"
        ROUTES["/slow"] = lambda h: (200, {"Content-Type": "text/html"}, body)

        # Hop check, post-response check, then time runs out mid-stream.
        ticks = iter([0.0, 1.0, 2.0, 3.0] + [999.0] * 5000)
        provider = HttpWebFetchProvider(
            client=self.client,
            resolver=resolving({}),
            total_deadline=20.0,
            monotonic=lambda: next(ticks),
        )
        with self.assertRaises(WebRetrievalError) as caught:
            provider.fetch("http://example.com/slow")
        self.assertEqual(caught.exception.code, RETRIEVAL_TIMEOUT)

    def test_the_deadline_is_not_reset_by_each_redirect(self) -> None:
        for index in range(3):
            ROUTES[f"/r{index}"] = redirect(f"http://example.com/r{index + 1}")
        ROUTES["/r3"] = page("<html><body><p>ordinary article text</p></body></html>")

        ticks = iter([0.0, 5.0, 10.0, 15.0, 21.0] + [50.0] * 50)
        provider = HttpWebFetchProvider(
            client=self.client,
            resolver=resolving({}),
            total_deadline=20.0,
            monotonic=lambda: next(ticks),
        )
        with self.assertRaises(WebRetrievalError) as caught:
            provider.fetch("http://example.com/r0")
        self.assertEqual(caught.exception.code, RETRIEVAL_TIMEOUT)

    def test_a_prompt_response_inside_the_deadline_succeeds(self) -> None:
        ROUTES["/fast"] = page("<html><body><p>ordinary article text</p></body></html>")
        provider = HttpWebFetchProvider(
            client=self.client,
            resolver=resolving({}),
            total_deadline=20.0,
            monotonic=lambda: 0.0,
        )
        self.assertIn("ordinary", provider.fetch("http://example.com/fast").content)


class ContentTypeCompositionTests(ComposedTestCase):
    """Missing and unparseable content types fail closed."""

    def test_an_absent_content_type_is_refused(self) -> None:
        ROUTES["/none"] = lambda h: (200, {}, b"<html><body><p>hi</p></body></html>")
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch("http://example.com/none")
        self.assertEqual(caught.exception.code, UNSUPPORTED_CONTENT_TYPE)

    def test_an_empty_content_type_is_refused(self) -> None:
        for value in ("", "   ", ";charset=utf-8"):
            with self.subTest(value=value):
                ROUTES["/e"] = lambda h, v=value: (
                    200, {"Content-Type": v}, b"<html><body>x</body></html>"
                )
                with self.assertRaises(WebRetrievalError) as caught:
                    self.provider().fetch("http://example.com/e")
                self.assertEqual(caught.exception.code, UNSUPPORTED_CONTENT_TYPE)

    def test_binary_content_is_not_returned_because_it_decodes(self) -> None:
        ROUTES["/bin"] = lambda h: (
            200, {"Content-Type": "application/octet-stream"}, bytes(range(256)) * 8
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch("http://example.com/bin")
        self.assertEqual(caught.exception.code, UNSUPPORTED_CONTENT_TYPE)


class ConcatenatedGzipTests(ComposedTestCase):
    """I: complete, or a truthful failure. Never a silent partial page."""

    def serve(self, body: bytes) -> None:
        ROUTES["/z"] = lambda h: (
            200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, body
        )

    def test_two_members_are_both_read(self) -> None:
        self.serve(
            gzip.compress(b"<html><body><p>FIRSTPART of the article ")
            + gzip.compress(b"and SECONDPART of the article.</p></body></html>")
        )
        content = self.provider().fetch("http://example.com/z").content
        self.assertIn("FIRSTPART", content)
        self.assertIn("SECONDPART", content)

    def test_many_members_are_all_read(self) -> None:
        parts = [gzip.compress(f"<p>member{i} text </p>".encode()) for i in range(6)]
        self.serve(b"".join(parts))
        content = self.provider().fetch("http://example.com/z").content
        for index in range(6):
            self.assertIn(f"member{index}", content)

    def test_a_partial_page_is_never_reported_as_complete(self) -> None:
        self.serve(
            gzip.compress(b"<html><body><p>FIRSTPART ")
            + gzip.compress(b"SECONDPART</p></body></html>")
        )
        result = self.provider().fetch("http://example.com/z")
        self.assertIn("SECONDPART", result.content)
        self.assertEqual(result.content_omitted_characters, 0)

    def test_a_truncated_member_fails_rather_than_returning_part(self) -> None:
        self.serve(gzip.compress(b"<html><body><p>truncated</p></body></html>")[:-6])
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch("http://example.com/z")
        self.assertEqual(caught.exception.code, PROVIDER_FAILED)

    def test_members_are_bounded_in_aggregate(self) -> None:
        """Many small members must not add up past the ceiling."""
        member = zlib.compress(b"A" * (1024 * 1024))
        ROUTES["/z"] = lambda h: (
            200,
            {"Content-Type": "text/html", "Content-Encoding": "deflate"},
            member * 6,
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.provider().fetch("http://example.com/z")
        self.assertEqual(caught.exception.code, PAGE_TOO_LARGE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
