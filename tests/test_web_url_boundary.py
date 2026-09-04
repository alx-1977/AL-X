"""The D-025 public-web boundary, proved without touching a network.

Resolution is injected in every test here. A boundary whose proof required a
live DNS answer could not be tested for the cases that matter most: the ones
where a hostname deliberately answers with an internal address.
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.web import URL_NOT_PUBLIC, WebRetrievalError
from alx.providers.web_url import is_public_address, parse_public_url


def resolving_to(*literals: str):
    """A resolver that answers with exactly these addresses."""

    def resolve(host: str, port: int):
        answers = []
        for literal in literals:
            family = socket.AF_INET6 if ":" in literal else socket.AF_INET
            answers.append(
                (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (literal, port))
            )
        return answers

    return resolve


PUBLIC = resolving_to("93.184.216.34")


class SchemeTests(unittest.TestCase):
    def test_https_is_accepted(self) -> None:
        url = parse_public_url("https://example.com/page", PUBLIC)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.host, "example.com")

    def test_http_is_accepted(self) -> None:
        self.assertEqual(
            parse_public_url("http://example.com/", PUBLIC).scheme, "http"
        )

    def test_non_web_schemes_are_refused(self) -> None:
        for raw in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "data:text/html,hello",
            "ws://example.com/",
            "wss://example.com/",
            "javascript:alert(1)",
            "//example.com/no-scheme",
            "example.com/no-scheme",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url(raw, PUBLIC)
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_blank_url_is_refused(self) -> None:
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRetrievalError):
                    parse_public_url(raw, PUBLIC)


class CredentialTests(unittest.TestCase):
    def test_a_credentialed_url_is_refused(self) -> None:
        for raw in (
            "https://user:pass@example.com/",
            "https://user@example.com/",
            "https://@example.com/",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url(raw, PUBLIC)
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)


class PortTests(unittest.TestCase):
    def test_default_ports_are_filled_in(self) -> None:
        self.assertEqual(parse_public_url("https://example.com/", PUBLIC).port, 443)
        self.assertEqual(parse_public_url("http://example.com/", PUBLIC).port, 80)

    def test_explicit_web_ports_are_accepted(self) -> None:
        self.assertEqual(
            parse_public_url("https://example.com:443/", PUBLIC).port, 443
        )
        self.assertEqual(
            parse_public_url("http://example.com:80/", PUBLIC).port, 80
        )

    def test_other_ports_are_refused(self) -> None:
        for port in (22, 25, 3306, 5432, 6379, 8080, 8443, 9200, 11211):
            with self.subTest(port=port):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url(f"https://example.com:{port}/", PUBLIC)
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)


class AddressClassificationTests(unittest.TestCase):
    def test_public_addresses_are_public(self) -> None:
        for literal in ("93.184.216.34", "8.8.8.8", "1.1.1.1",
                        "2606:2800:220:1:248:1893:25c8:1946"):
            with self.subTest(literal=literal):
                self.assertTrue(is_public_address(literal))

    def test_loopback_is_not_public(self) -> None:
        for literal in ("127.0.0.1", "127.1.2.3", "::1"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_private_ranges_are_not_public(self) -> None:
        for literal in ("10.0.0.1", "10.255.255.254", "172.16.0.1",
                        "172.31.255.254", "192.168.0.1", "192.168.1.1"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_link_local_and_metadata_are_not_public(self) -> None:
        for literal in ("169.254.0.1", "169.254.169.254", "fe80::1"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_unique_local_ipv6_is_not_public(self) -> None:
        for literal in ("fc00::1", "fd00::1"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_multicast_is_not_public(self) -> None:
        for literal in ("224.0.0.1", "239.255.255.250", "ff02::1"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_reserved_and_unspecified_are_not_public(self) -> None:
        for literal in ("0.0.0.0", "::", "240.0.0.1", "255.255.255.255"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_ipv4_mapped_ipv6_cannot_smuggle_a_private_address(self) -> None:
        """The spelling changes; the destination does not."""
        for literal in ("::ffff:127.0.0.1", "::ffff:10.0.0.1",
                        "::ffff:192.168.1.1", "::ffff:169.254.169.254"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))

    def test_sixtofour_cannot_smuggle_a_private_address(self) -> None:
        # 2002::/16 embeds an IPv4 address in the prefix.
        self.assertFalse(is_public_address("2002:0a00:0001::1"))
        self.assertFalse(is_public_address("2002:7f00:0001::1"))

    def test_nonsense_is_not_public(self) -> None:
        for literal in ("", "not-an-address", "999.999.999.999"):
            with self.subTest(literal=literal):
                self.assertFalse(is_public_address(literal))


class LiteralUrlTests(unittest.TestCase):
    def test_a_public_literal_needs_no_resolution(self) -> None:
        def explode(host, port):  # pragma: no cover - must not be called
            raise AssertionError("a literal address must not be resolved")

        url = parse_public_url("https://93.184.216.34/", explode)
        self.assertEqual(url.addresses[0].literal, "93.184.216.34")

    def test_private_literals_are_refused(self) -> None:
        for raw in (
            "http://127.0.0.1/",
            "http://localhost.example.com.127.0.0.1/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://[fc00::1]/",
            "http://[::ffff:127.0.0.1]/",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url(raw, resolving_to("127.0.0.1"))
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_public_ipv6_literal_is_accepted(self) -> None:
        url = parse_public_url("https://[2606:2800:220:1:248:1893:25c8:1946]/",
                               PUBLIC)
        self.assertEqual(url.addresses[0].family, socket.AF_INET6)


class ResolutionTests(unittest.TestCase):
    def test_a_hostname_resolving_privately_is_refused(self) -> None:
        for literal in ("127.0.0.1", "10.0.0.5", "192.168.1.9",
                        "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with self.subTest(literal=literal):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url("https://rebind.example/",
                                     resolving_to(literal))
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_mixed_answer_refuses_the_whole_hostname(self) -> None:
        """Choosing the public member would let the DNS answer choose."""
        with self.assertRaises(WebRetrievalError) as caught:
            parse_public_url(
                "https://mixed.example/", resolving_to("93.184.216.34", "10.0.0.1")
            )
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_mixed_answer_is_refused_in_either_order(self) -> None:
        with self.assertRaises(WebRetrievalError):
            parse_public_url(
                "https://mixed.example/", resolving_to("10.0.0.1", "93.184.216.34")
            )

    def test_every_public_address_is_kept(self) -> None:
        url = parse_public_url(
            "https://example.com/", resolving_to("93.184.216.34", "8.8.8.8")
        )
        self.assertEqual(
            [item.literal for item in url.addresses],
            ["93.184.216.34", "8.8.8.8"],
        )

    def test_any_kept_address_is_safe_to_connect_to(self) -> None:
        """The fetcher picks one; refusing mixed answers is what makes that safe."""
        from alx.providers.web_url import is_public_address

        url = parse_public_url(
            "https://example.com/", resolving_to("93.184.216.34", "8.8.8.8", "1.1.1.1")
        )
        for address in url.addresses:
            with self.subTest(address=address.literal):
                self.assertTrue(is_public_address(address.literal))

    def test_duplicate_answers_are_collapsed(self) -> None:
        url = parse_public_url(
            "https://example.com/", resolving_to("93.184.216.34", "93.184.216.34")
        )
        self.assertEqual(len(url.addresses), 1)

    def test_an_empty_answer_is_refused(self) -> None:
        with self.assertRaises(WebRetrievalError) as caught:
            parse_public_url("https://example.com/", resolving_to())
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_a_failing_resolver_is_refused_not_raised(self) -> None:
        def fails(host, port):
            raise socket.gaierror("nope")

        with self.assertRaises(WebRetrievalError) as caught:
            parse_public_url("https://example.com/", fails)
        self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)

    def test_resolution_happens_exactly_once(self) -> None:
        """One lookup is what closes the rebinding window."""
        calls = []

        def counting(host, port):
            calls.append((host, port))
            return PUBLIC(host, port)

        parse_public_url("https://example.com/", counting)
        self.assertEqual(len(calls), 1)

    def test_a_single_label_hostname_is_refused(self) -> None:
        for raw in ("http://localhost/", "http://intranet/", "http://router/"):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRetrievalError) as caught:
                    parse_public_url(raw, PUBLIC)
                self.assertEqual(caught.exception.code, URL_NOT_PUBLIC)


class NormalisationTests(unittest.TestCase):
    def test_the_host_is_lowercased_and_the_trailing_dot_removed(self) -> None:
        url = parse_public_url("https://EXAMPLE.com./page", PUBLIC)
        self.assertEqual(url.host, "example.com")

    def test_an_empty_path_becomes_root(self) -> None:
        self.assertTrue(parse_public_url("https://example.com", PUBLIC).url.endswith("/"))

    def test_the_fragment_is_dropped(self) -> None:
        url = parse_public_url("https://example.com/p?a=1#frag", PUBLIC)
        self.assertNotIn("#", url.url)
        self.assertIn("a=1", url.url)

    def test_the_source_domain_is_the_host(self) -> None:
        self.assertEqual(
            parse_public_url("https://example.com/x", PUBLIC).source_domain,
            "example.com",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
