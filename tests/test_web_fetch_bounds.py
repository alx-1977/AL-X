"""Bounded page retrieval, proved against a local fixture server.

Nothing here reaches the internet. A fixture server on loopback serves the
awkward responses — a lying Content-Length, a compression bomb, a redirect
walking toward a private address — and the resolver is injected so the
boundary still believes it is talking to a public host.
"""

from __future__ import annotations

import gzip
import socket
import sys
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from alx.contracts.web import (
    MAX_DOWNLOAD_BYTES,
    PAGE_TOO_LARGE,
    PROVIDER_FAILED,
    RETRIEVAL_BLOCKED,
    UNSUPPORTED_CONTENT_TYPE,
    UNSUPPORTED_DYNAMIC_PAGE,
    URL_NOT_PUBLIC,
    WebRetrievalError,
)
from alx.providers.web_fetch import HttpWebFetchProvider, extract


ROUTES: dict = {}


class FixtureServer(ThreadingTCPServer):
    """Threaded so a keep-alive connection cannot block the next request."""

    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Stay quiet when a bound test hangs up mid-body.

        Aborting an oversized download is the behaviour under test, so the
        reset it causes is expected. Printing a traceback for it would bury a
        real failure in noise.
        """


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        route = ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, headers, body = route(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def page(html: str, content_type: str = "text/html; charset=utf-8"):
    return lambda h: (200, {"Content-Type": content_type}, html.encode())


class FetchTestCase(unittest.TestCase):
    """Serves fixtures on loopback while the boundary sees a public host."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FixtureServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        ROUTES.clear()
        # The fixture listens on loopback, so the pinned connection must go
        # there while validation is exercised against a public answer.
        self.client = httpx.Client(
            timeout=5.0,
            follow_redirects=False,
            transport=LoopbackTransport(self.port),
        )
        self.provider = HttpWebFetchProvider(
            client=self.client, resolver=public_resolver
        )

    def tearDown(self) -> None:
        self.client.close()

    def fetch(self, path: str = "/", **kwargs):
        return self.provider.fetch(f"http://fixture.example{path}", **kwargs)


def public_resolver(host: str, port: int):
    """Every fixture hostname resolves to one public address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("93.184.216.34", port))]


def private_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("10.0.0.1", port))]


class LoopbackTransport(httpx.BaseTransport):
    """Send the pinned request to the fixture, preserving what it carried.

    The provider builds a request aimed at a validated public address. The
    fixture is on loopback, so the transport redirects the socket while
    keeping the Host header and sni_hostname the provider set, which is what
    the assertions below inspect.
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx.HTTPTransport()
        self.seen: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        moved = request.url.copy_with(host="127.0.0.1", port=self._port, scheme="http")
        forwarded = httpx.Request(
            request.method, moved, headers=request.headers, extensions=request.extensions
        )
        return self._inner.handle_request(forwarded)


class SuccessTests(FetchTestCase):
    def test_a_simple_page_is_retrieved_with_provenance(self) -> None:
        ROUTES["/"] = page(
            "<html><head><title>Water Levels</title>"
            "<meta property='og:site_name' content='Example News'>"
            "</head><body><p>The reservoir is at sixty percent.</p></body></html>"
        )
        result = self.fetch()
        self.assertEqual(result.title, "Water Levels")
        self.assertEqual(result.publisher, "Example News")
        self.assertEqual(result.source_domain, "fixture.example")
        self.assertIn("sixty percent", result.content)
        self.assertEqual(result.http_status, 200)
        self.assertIsNotNone(result.retrieved_at.tzinfo)

    def test_the_requested_url_is_preserved(self) -> None:
        ROUTES["/page"] = page("<html><body><p>hello</p></body></html>")
        result = self.fetch("/page")
        self.assertEqual(result.requested_url, "http://fixture.example/page")

    def test_plain_text_is_returned_as_is(self) -> None:
        ROUTES["/t"] = page("just words", "text/plain")
        self.assertEqual(self.fetch("/t").content, "just words")

    def test_script_and_navigation_are_dropped(self) -> None:
        ROUTES["/"] = page(
            "<html><body><nav>Menu Home About</nav>"
            "<script>var secret='tracking';</script>"
            "<p>Real content here.</p><footer>Copyright</footer></body></html>"
        )
        content = self.fetch().content
        self.assertIn("Real content", content)
        self.assertNotIn("tracking", content)
        self.assertNotIn("Menu", content)
        self.assertNotIn("Copyright", content)

    def test_gzip_is_decompressed(self) -> None:
        body = gzip.compress(b"<html><body><p>compressed words</p></body></html>")
        ROUTES["/z"] = lambda h: (
            200,
            {"Content-Type": "text/html", "Content-Encoding": "gzip"},
            body,
        )
        self.assertIn("compressed words", self.fetch("/z").content)


class HostAndSniTests(FetchTestCase):
    def test_the_original_hostname_survives_pinning(self) -> None:
        """The connection is pinned, but the certificate name is not."""
        ROUTES["/"] = page("<html><body><p>x</p></body></html>")
        self.fetch()
        request = self.client._transport.seen[-1]
        self.assertEqual(request.headers["Host"], "fixture.example")
        self.assertEqual(
            request.extensions.get("sni_hostname"), "fixture.example"
        )

    def test_the_connection_targets_the_validated_address(self) -> None:
        ROUTES["/"] = page("<html><body><p>x</p></body></html>")
        self.fetch()
        request = self.client._transport.seen[-1]
        self.assertEqual(request.url.host, "93.184.216.34")


class TruncationTests(FetchTestCase):
    def test_long_content_is_cut_and_the_shortfall_reported(self) -> None:
        ROUTES["/"] = page("<html><body><p>" + ("a" * 9000) + "</p></body></html>")
        result = self.fetch(max_characters=1000)
        self.assertEqual(len(result.content), 1000)
        self.assertEqual(result.content_omitted_characters, 8000)

    def test_a_short_page_omits_nothing(self) -> None:
        ROUTES["/"] = page("<html><body><p>short</p></body></html>")
        self.assertEqual(self.fetch().content_omitted_characters, 0)

    def test_the_ceiling_cannot_be_raised_by_argument(self) -> None:
        ROUTES["/"] = page("<html><body><p>" + ("b" * 20000) + "</p></body></html>")
        result = self.fetch(max_characters=999_999)
        self.assertLessEqual(len(result.content), 8000)
        self.assertGreater(result.content_omitted_characters, 0)


class BoundTests(FetchTestCase):
    def test_a_lying_content_length_does_not_defeat_the_counter(self) -> None:
        """The declared size is a claim; the counted bytes are the fact."""
        huge = b"<html><body>" + b"x" * (MAX_DOWNLOAD_BYTES + 5000) + b"</body></html>"
        ROUTES["/big"] = lambda h: (
            200,
            {"Content-Type": "text/html", "Content-Length": str(len(huge))},
            huge,
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/big")
        self.assertEqual(caught.exception.code, PAGE_TOO_LARGE)

    def test_a_compression_bomb_is_refused(self) -> None:
        bomb = zlib.compress(b"A" * (MAX_DOWNLOAD_BYTES * 4))
        ROUTES["/bomb"] = lambda h: (
            200,
            {"Content-Type": "text/html", "Content-Encoding": "deflate"},
            bomb,
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/bomb")
        self.assertEqual(caught.exception.code, PAGE_TOO_LARGE)


class ContentTypeTests(FetchTestCase):
    def test_non_textual_types_are_refused(self) -> None:
        for media in ("application/pdf", "image/png", "application/zip",
                      "application/octet-stream", "video/mp4"):
            with self.subTest(media=media):
                ROUTES["/f"] = lambda h, m=media: (200, {"Content-Type": m}, b"data")
                with self.assertRaises(WebRetrievalError) as caught:
                    self.fetch("/f")
                self.assertEqual(caught.exception.code, UNSUPPORTED_CONTENT_TYPE)

    def test_allowed_types_pass(self) -> None:
        for media in ("text/html", "application/xhtml+xml", "text/plain"):
            with self.subTest(media=media):
                ROUTES["/a"] = page("<html><body><p>fine</p></body></html>", media)
                self.assertIn("fine", self.fetch("/a").content)


class RedirectTests(FetchTestCase):
    def _redirect(self, path: str, target: str, status: int = 302) -> None:
        ROUTES[path] = lambda h: (status, {"Location": target, "Content-Length": "0"}, b"")

    def test_a_public_redirect_is_followed(self) -> None:
        self._redirect("/from", "http://fixture.example/to")
        ROUTES["/to"] = page("<html><body><p>arrived</p></body></html>")
        result = self.fetch("/from")
        self.assertIn("arrived", result.content)
        self.assertEqual(result.requested_url, "http://fixture.example/from")
        self.assertTrue(result.final_url.endswith("/to"))

    def test_a_redirect_toward_a_private_address_is_refused(self) -> None:
        """The hop is revalidated, so the walk inward stops here."""
        self._redirect("/evil", "http://internal.example/secrets")

        def mixed(host: str, port: int):
            return (private_resolver if host == "internal.example"
                    else public_resolver)(host, port)

        provider = HttpWebFetchProvider(client=self.client, resolver=mixed)
        with self.assertRaises(WebRetrievalError) as caught:
            provider.fetch("http://fixture.example/evil")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_redirect_to_a_non_web_scheme_is_refused(self) -> None:
        self._redirect("/s", "file:///etc/passwd")
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/s")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_redirect_to_a_forbidden_port_is_refused(self) -> None:
        self._redirect("/p", "http://fixture.example:22/")
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/p")
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_more_than_three_redirects_are_refused(self) -> None:
        for index in range(6):
            self._redirect(f"/r{index}", f"http://fixture.example/r{index + 1}")
        ROUTES["/r6"] = page("<html><body><p>end</p></body></html>")
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/r0")
        self.assertEqual(caught.exception.code, RETRIEVAL_BLOCKED)

    def test_exactly_three_redirects_are_allowed(self) -> None:
        for index in range(3):
            self._redirect(f"/h{index}", f"http://fixture.example/h{index + 1}")
        ROUTES["/h3"] = page("<html><body><p>reached</p></body></html>")
        self.assertIn("reached", self.fetch("/h0").content)

    def test_a_redirect_loop_is_refused(self) -> None:
        self._redirect("/loop", "http://fixture.example/loop")
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/loop")
        self.assertEqual(caught.exception.code, RETRIEVAL_BLOCKED)

    def test_a_redirect_without_a_location_is_refused(self) -> None:
        ROUTES["/nowhere"] = lambda h: (302, {"Content-Length": "0"}, b"")
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/nowhere")
        self.assertEqual(caught.exception.code, RETRIEVAL_BLOCKED)


class StatusTests(FetchTestCase):
    def test_blocked_statuses_report_blocked(self) -> None:
        for status in (401, 403, 429):
            with self.subTest(status=status):
                ROUTES["/b"] = lambda h, s=status: (s, {"Content-Type": "text/html"}, b"no")
                with self.assertRaises(WebRetrievalError) as caught:
                    self.fetch("/b")
                self.assertEqual(caught.exception.code, RETRIEVAL_BLOCKED)

    def test_a_server_error_reports_provider_failure(self) -> None:
        for status in (404, 500, 503):
            with self.subTest(status=status):
                ROUTES["/e"] = lambda h, s=status: (s, {"Content-Type": "text/html"}, b"x")
                with self.assertRaises(WebRetrievalError) as caught:
                    self.fetch("/e")
                self.assertEqual(caught.exception.code, PROVIDER_FAILED)

    def test_blocked_and_failed_stay_distinguishable(self) -> None:
        """Collapsing them would decide what the refusal meant."""
        self.assertNotEqual(RETRIEVAL_BLOCKED, PROVIDER_FAILED)


class DynamicPageTests(FetchTestCase):
    def test_a_script_shell_is_reported_not_returned_blank(self) -> None:
        ROUTES["/app"] = page(
            "<html><body><div id='root'></div>"
            "<script>" + ("var x=1;" * 400) + "</script></body></html>"
        )
        with self.assertRaises(WebRetrievalError) as caught:
            self.fetch("/app")
        self.assertEqual(caught.exception.code, UNSUPPORTED_DYNAMIC_PAGE)

    def test_a_real_page_with_scripts_is_still_read(self) -> None:
        ROUTES["/mixed"] = page(
            "<html><body><script>" + ("var y=2;" * 400) + "</script>"
            "<p>" + ("Genuine article text. " * 40) + "</p></body></html>"
        )
        self.assertIn("Genuine article", self.fetch("/mixed").content)


class ExtractionTests(unittest.TestCase):
    """Extraction is pure, so it needs no server at all."""

    def test_entities_are_unescaped(self) -> None:
        text, _, _, _, _ = extract("<html><body><p>Tom &amp; Jerry</p></body></html>")
        self.assertIn("Tom & Jerry", text)

    def test_malformed_markup_still_yields_what_parsed(self) -> None:
        text, _, _, _, _ = extract("<html><body><p>before<<<>>garbage")
        self.assertIn("before", text)

    def test_a_canonical_link_is_recovered(self) -> None:
        _, _, _, canonical, _ = extract(
            "<html><head><link rel='canonical' href='https://a.example/x'>"
            "</head><body>hi</body></html>"
        )
        self.assertEqual(canonical, "https://a.example/x")

    def test_og_title_is_used_when_there_is_no_title_element(self) -> None:
        _, title, _, _, _ = extract(
            "<html><head><meta property='og:title' content='Open Graph Name'>"
            "</head><body>x</body></html>"
        )
        self.assertEqual(title, "Open Graph Name")

    def test_block_elements_become_line_breaks(self) -> None:
        text, _, _, _, _ = extract("<html><body><p>one</p><p>two</p></body></html>")
        self.assertIn("one", text)
        self.assertIn("two", text)
        self.assertNotIn("onetwo", text)

    def test_script_volume_is_counted_but_not_kept(self) -> None:
        text, _, _, _, scripts = extract(
            "<html><body><script>abcdefghij</script><p>kept</p></body></html>"
        )
        self.assertNotIn("abcdefghij", text)
        self.assertGreaterEqual(scripts, 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
