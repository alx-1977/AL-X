"""D-045: AL/X has full access to Friedl's Particle account.

These tests prove any Particle Cloud API call can be made and its answer,
refusals included, comes back as Particle gave it; the token only ever goes
to Particle's API address; event streams are refused; replies are capped;
usage is read from Particle's report (request, wait, download, parse) without
sending the token to the download address; every change and report is
recorded; and the access is its own permission with no per-call approval.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.readers import PARTICLE_FULL_PERMISSION, build_reader_runtime  # noqa: E402
from alx.config import ReaderSettings  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.readers import ReaderAccessError  # noqa: E402
from alx.providers.particle import ParticleCloud, parse_usage_csv  # noqa: E402
from alx.tools.particle import (  # noqa: E402
    PARTICLE_API_REQUEST,
    READ_PARTICLE_USAGE,
    build_particle_executors,
)

NOW = datetime(2026, 10, 8, 17, tzinfo=UTC)
CSV = """Query date: 2026-10-08
Account queried: friedl@fire-fli.co.za
"Query type: All Devices, 1 days of data"
""
Date,Device ID,Device Name,Product ID,Product Name,Connectivity,SIM ICCID,Data Operations,Firmware Version,Device Group,Device OS
2026-10-07,e00fce683341b2e6ab2d5218,ALX_1,45984,B5Som Prototypes,cellular,8988,125,65535,"",6.4.1
2026-10-07,e00fce68bdddfd05c471cf5a,ALX_2,45984,B5Som Prototypes,cellular,8989,3,16,"",6.4.1
"""


def reply(status=200, body=None, content=None, chunk=65536, read=None):
    """A streamed Particle reply; `read` counts the chunks actually taken."""
    data = content if content is not None else (
        b"" if body is None else __import__("json").dumps(body).encode())
    response = Mock(status_code=status)

    def chunks():
        for index in range(0, len(data), chunk):
            if read is not None:
                read.append(1)
            yield data[index:index + chunk]

    response.iter_bytes.side_effect = lambda: chunks()
    stream = MagicMock()
    stream.__enter__.return_value = response
    return stream


class ApiTests(unittest.TestCase):
    def test_any_call_returns_particles_status_and_reply(self) -> None:
        with patch("httpx.stream", return_value=reply(200, {"ok": True})) as sent:
            status, body = ParticleCloud("token").api(
                "POST", "/v1/products/45984/devices/abc/restart", {"x": "1"}, {"a": 1})
        self.assertEqual((status, body), (200, {"ok": True}))
        method, url = sent.call_args.args
        self.assertEqual((method, url),
                         ("POST", "https://api.particle.io/v1/products/45984/devices/abc/restart"))
        self.assertEqual(sent.call_args.kwargs["json"], {"a": 1})
        self.assertEqual(sent.call_args.kwargs["headers"], {"Authorization": "Bearer token"})

    def test_a_refusal_comes_back_rather_than_hidden(self) -> None:
        with patch("httpx.stream", return_value=reply(404, {"error": "Not Found"})):
            self.assertEqual(ParticleCloud("token").api("DELETE", "/v1/products/1/devices/x"),
                             (404, {"error": "Not Found"}))

    def test_the_token_never_leaves_particles_address(self) -> None:
        for path in ("https://evil.example/v1/x", "/v2/devices", "v1/devices",
                     "/v1/../admin", "/v1/devices?access_token=x", "//evil/v1/"):
            with self.subTest(path=path):
                with patch("httpx.stream") as sent:
                    with self.assertRaises(ReaderAccessError):
                        ParticleCloud("token").api("GET", path)
                sent.assert_not_called()

    def test_unknown_methods_and_event_streams_are_refused(self) -> None:
        with patch("httpx.stream") as sent:
            for method, path in (("TRACE", "/v1/devices"), ("GET", "/v1/products/1/events"),
                                 ("GET", "/v1/events/roomreader")):
                with self.subTest(method=method, path=path):
                    with self.assertRaises(ReaderAccessError):
                        ParticleCloud("token").api(method, path)
        sent.assert_not_called()

    def test_a_huge_reply_is_capped_while_it_is_read(self) -> None:
        read = []
        with patch("httpx.stream", return_value=reply(200, content=b"x" * 10_000_000, read=read)):
            _status, body = ParticleCloud("token").api("GET", "/v1/devices")
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["partial"]), 1_000_000)
        # Stopped just past the cap, not after all 10 MB.
        self.assertLess(len(read), 20)


class UsageTests(unittest.TestCase):
    def test_the_report_is_requested_awaited_downloaded_and_read(self) -> None:
        agreements = {"data": [{"id": "293802", "attributes": {"state": "active"}}]}
        created = {"data": {"id": "49890", "attributes": {"state": "pending"}}}
        pending = {"data": {"attributes": {"state": "pending"}}}
        ready = {"data": {"attributes": {"state": "available",
                                          "download_url": "https://storage.example/r.csv"}}}
        calls = []
        requested_seen = []

        def api(method, url, **kwargs):
            calls.append((method, url))
            requested_seen.append(kwargs.get("timeout"))
            if url.endswith("/v1/user/service_agreements"):
                return reply(200, agreements)
            if url.endswith("/usage_reports") and method == "POST":
                self.assertEqual(kwargs["json"], {"report_type": "devices",
                                                  "date_period_start": "2026-10-07",
                                                  "date_period_end": "2026-10-07"})
                return reply(201, created)
            return reply(200, pending if len(calls) < 4 else ready)

        download = Mock(status_code=200, text=CSV)
        reported = []
        with patch("httpx.stream", side_effect=api), \
                patch("httpx.get", return_value=download) as fetched:
            rows = ParticleCloud("token").usage("2026-10-07", "2026-10-07", sleep=lambda s: None,
                                                on_requested=reported.append)
        self.assertEqual(reported, ["49890"])
        self.assertEqual([(r["device_name"], r["data_operations"]) for r in rows],
                         [("ALX_1", 125), ("ALX_2", 3)])
        self.assertIn(("POST", "https://api.particle.io/v1/user/service_agreements/293802/usage_reports"), calls)
        # The pre-signed download carries no token.
        self.assertNotIn("headers", fetched.call_args.kwargs)

    def test_the_deadline_counts_time_spent_waiting_on_particle(self) -> None:
        pending = {"data": {"id": "1", "attributes": {"state": "pending"}}}
        clock = [0.0]

        def slow(method, url, **kwargs):
            clock[0] += 10  # every request takes 10 s
            if url.endswith("/v1/user/service_agreements"):
                return reply(200, {"data": [{"id": "9", "attributes": {"state": "active"}}]})
            return reply(201 if method == "POST" else 200, pending)

        def sleep(seconds):
            clock[0] += seconds

        with patch("httpx.stream", side_effect=slow), \
                patch("alx.providers.particle.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ReaderAccessError) as raised:
                ParticleCloud("token").usage("2026-10-07", "2026-10-07", wait_seconds=120,
                                             sleep=sleep)
        self.assertEqual(raised.exception.code, "usage_not_ready")
        self.assertLessEqual(clock[0], 140)

    def test_a_gateway_error_page_is_unavailable_not_a_fault(self) -> None:
        with patch("httpx.stream", return_value=reply(502, content=b"<html>Bad gateway</html>")):
            with self.assertRaises(ReaderAccessError) as raised:
                ParticleCloud("token").usage("2026-10-07", "2026-10-07")
        self.assertEqual(raised.exception.code, "usage_unavailable")

    def test_bad_dates_are_refused_before_any_call(self) -> None:
        with patch("httpx.stream") as sent:
            for start, end in (("yesterday", "2026-10-07"), ("2026-10-08", "2026-10-07")):
                with self.assertRaises(ReaderAccessError):
                    ParticleCloud("token").usage(start, end)
        sent.assert_not_called()

    def test_the_csv_preamble_is_skipped(self) -> None:
        self.assertEqual(len(parse_usage_csv(CSV)), 2)
        with self.assertRaises(ReaderAccessError):
            parse_usage_csv("no header here")


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logged = []
        self.particle = Mock()
        self.execute = build_particle_executors(
            self.particle, lambda at, uid, kind, detail: self.logged.append((kind, dict(detail))),
            lambda: "call-1", clock=lambda: NOW)

    def test_a_change_is_recorded_and_a_read_is_not(self) -> None:
        self.particle.api.return_value = (200, {"ok": True})
        self.execute[PARTICLE_API_REQUEST]({"method": "get", "path": "/v1/devices"})
        result = self.execute[PARTICLE_API_REQUEST](
            {"method": "PUT", "path": "/v1/products/45984/devices/x", "body": {"name": "R1"}})
        self.assertEqual(result.values, {"status": 200, "body": {"ok": True}})
        self.assertEqual(self.logged, [
            ("particle_api_attempt", {"method": "PUT", "path": "/v1/products/45984/devices/x"}),
            ("particle_api", {"method": "PUT", "path": "/v1/products/45984/devices/x",
                              "status": 200})])

    def test_a_change_whose_reply_is_lost_is_still_recorded(self) -> None:
        self.particle.api.side_effect = ReaderAccessError("device_timeout")
        self.execute[PARTICLE_API_REQUEST]({"method": "DELETE", "path": "/v1/sims/123"})
        self.assertEqual([kind for kind, _ in self.logged],
                         ["particle_api_attempt", "particle_api"])
        self.assertTrue(self.logged[1][1]["outcome_unknown"])

    def test_a_report_requested_but_not_downloaded_is_recorded(self) -> None:
        def usage(start, end, devices, on_requested=None):
            on_requested("49890")
            raise ReaderAccessError("connection_failed")

        self.particle.usage.side_effect = usage
        result = self.execute[READ_PARTICLE_USAGE]({"start": "2026-10-07", "end": "2026-10-07"})
        self.assertEqual(result.failure["code"], "connection_failed")
        self.assertEqual([kind for kind, _ in self.logged],
                         ["particle_usage_requested", "particle_usage_report"])

    def test_usage_is_returned_with_its_total_and_recorded(self) -> None:
        self.particle.usage.return_value = parse_usage_csv(CSV)
        result = self.execute[READ_PARTICLE_USAGE]({"start": "2026-10-07", "end": "2026-10-07"})
        self.assertEqual(result.values["total_data_operations"], 128)
        self.assertEqual(self.logged[0][0], "particle_usage_report")

    def test_a_refused_call_is_a_declared_failure(self) -> None:
        self.particle.api.side_effect = ReaderAccessError("arguments_unusable")
        result = self.execute[PARTICLE_API_REQUEST]({"method": "GET", "path": "https://x"})
        self.assertEqual((result.state, result.failure["code"]),
                         (CapabilityResultState.FAILED, "arguments_unusable"))


class AuthorityTests(unittest.TestCase):
    def test_full_access_is_its_own_permission_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_reader_runtime(ReaderSettings.from_environment({
                "PARTICLE_ACCESS_TOKEN": "t", "ALX_READER_PRODUCT_IDS": "45984"}),
                Path(directory), lambda: "c")
            runtime.calendar.close()
        for capability in (PARTICLE_API_REQUEST, READ_PARTICLE_USAGE):
            policy = runtime.policies[capability]
            self.assertEqual(policy.permission_references, frozenset({PARTICLE_FULL_PERMISSION}))
            self.assertFalse(policy.approval_required)
        self.assertIn(PARTICLE_FULL_PERMISSION, runtime.permissions)


if __name__ == "__main__":
    unittest.main()
