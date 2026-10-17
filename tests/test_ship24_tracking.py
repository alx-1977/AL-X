"""Ship24's documented envelope and error codes, with HTTP fully mocked."""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.ship24 import build_ship24_runtime  # noqa: E402
from alx.contracts import CapabilityResultState, SideEffect  # noqa: E402
from alx.core.model_reasoner import _catalogue_payload  # noqa: E402
from alx.providers.ship24 import Ship24Provider  # noqa: E402
from alx.tools.ship24 import DEFINITION, TRACK_SHIPMENT  # noqa: E402


def response(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.ship24.com/public/v1/tracking/search"))


TRACKED = {
    "data": {"trackings": [{
        "shipment": {
            "statusCode": "delivery_delivered", "statusMilestone": "delivered",
            "delivery": {"estimatedDeliveryDate": "2026-10-18T18:00:00Z"},
        },
        "events": [
            {"occurrenceDatetime": "2026-10-17T12:00:00Z", "location": "Cape Town",
             "status": "Delivered to recipient", "statusCode": "delivery_delivered",
             "courierCode": "ups"},
            {"occurrenceDatetime": "2026-10-16T12:00:00Z", "location": None,
             "status": "In transit", "statusCode": "transit_handover",
             "courierCode": "ups"},
        ],
    }]}
}


class Ship24TrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = build_ship24_runtime(lambda: "call-1")
        self.execute = self.runtime.executors[TRACK_SHIPMENT]

    def test_catalogue_and_runtime_contract(self) -> None:
        self.assertEqual(self.runtime.definitions, (DEFINITION,))
        self.assertIs(DEFINITION.side_effect, SideEffect.EFFECTFUL)
        self.assertEqual(DEFINITION.input_schema.required, ("tracking_number",))
        self.assertIn("courier_code", DEFINITION.input_schema.properties)
        self.assertIn("events", DEFINITION.output_schema.properties)
        self.assertIn("tracking_not_found", DEFINITION.possible_failure_codes)
        catalogue = json.loads(_catalogue_payload(self.runtime.definitions))
        entry = catalogue["capabilities"][0]
        self.assertEqual(entry["id"], TRACK_SHIPMENT)
        self.assertEqual(entry["side_effect"], "effectful")
        self.assertEqual(entry["input_schema"]["required"], ["tracking_number"])
        self.assertIn("events", entry["result_fields"])
        from alx.bootstrap import ship24
        from pathlib import Path
        decision = (Path(__file__).resolve().parents[1] / "governance/DECISIONS.md").read_text()
        self.assertIn("D-056 — AL/X tracks parcels within Ship24's free plan", decision)
        self.assertIn("D-056", ship24.__doc__)
        self.assertIn("tracking_allowance_exhausted", DEFINITION.possible_failure_codes)
        self.assertIn("per-call lookup billed by Ship24", DEFINITION.purpose)
        self.assertIn("fetches tracking from the courier directly", DEFINITION.purpose)

    def test_no_production_reference_to_tracker_endpoints(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "alx"
        for path in source_root.rglob("*.py"):
            with self.subTest(path=path):
                source = path.read_text()
                self.assertNotIn("/public/v1/trackers", source)
                self.assertNotIn("/trackers/track", source)

    def test_exhausted_allowance_is_durable_and_stops_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ship24.sqlite3"
            runtime = build_ship24_runtime(lambda: "call-1", {"SHIP24_API_KEY": "example-token"}, path)
            with patch("alx.providers.ship24.httpx.post", return_value=response(
                429, {"errors": [{"code": "plan_limit_exceeded", "message": "Monthly tracking quota exhausted"}]}
            )) as post:
                first = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "A"})
                second = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "B"})
            self.assertEqual(first.failure["code"], "tracking_allowance_exhausted")
            self.assertEqual(second.failure["code"], "tracking_allowance_exhausted")
            post.assert_called_once()
            restarted = build_ship24_runtime(lambda: "call-2", {"SHIP24_API_KEY": "example-token"}, path)
            with patch("alx.providers.ship24.httpx.post") as post:
                result = restarted.executors[TRACK_SHIPMENT]({"tracking_number": "C"})
            self.assertEqual(result.failure["code"], "tracking_allowance_exhausted")
            post.assert_not_called()

    def test_last_successful_lookup_uses_allowance_and_blocks_next(self) -> None:
        runtime = build_ship24_runtime(lambda: "call-1", {"SHIP24_API_KEY": "example-token"})
        last = response(200, TRACKED)
        last.headers["x-ship24-monthly-remaining"] = "0"
        with patch("alx.providers.ship24.httpx.post", return_value=last) as post:
            first = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "A"})
            second = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "B"})
        self.assertIs(first.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(second.failure["code"], "tracking_allowance_exhausted")
        post.assert_called_once()

    def test_new_month_allows_tracking_after_prior_month_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ship24.sqlite3"
            runtime = build_ship24_runtime(lambda: "call-1", {"SHIP24_API_KEY": "example-token"}, path)
            with patch.object(Ship24Provider, "_month", return_value="2026-10"), patch(
                "alx.providers.ship24.httpx.post",
                return_value=response(429, {"errors": [{"code": "quota_exceeded"}]}),
            ):
                self.assertEqual(runtime.executors[TRACK_SHIPMENT]({"tracking_number": "A"}).failure["code"],
                                 "tracking_allowance_exhausted")
            restarted = build_ship24_runtime(lambda: "call-2", {"SHIP24_API_KEY": "example-token"}, path)
            with patch.object(Ship24Provider, "_month", return_value="2026-11"), patch(
                "alx.providers.ship24.httpx.post", return_value=response(200, TRACKED),
            ) as post:
                result = restarted.executors[TRACK_SHIPMENT]({"tracking_number": "A"})
            self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
            post.assert_called_once()

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_documented_request_and_response(self) -> None:
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            result = self.execute({"tracking_number": "1Z999AA10123456784", "courier_code": "ups"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(DEFINITION.output_schema.accepts(result.values))
        self.assertEqual(result.values["status"], "delivery_delivered")
        self.assertEqual(result.values["tracking_number"], "1Z999AA10123456784")
        self.assertEqual(result.values["courier"], "ups")
        self.assertEqual(result.values["status_milestone"], "delivered")
        self.assertEqual(result.values["estimated_delivery"], "2026-10-18T18:00:00Z")
        self.assertEqual(result.values["last_update"], "2026-10-17T12:00:00Z")
        self.assertEqual(len(result.values["events"]), 2)
        self.assertEqual(result.values["events"][0]["description"], "Delivered to recipient")
        self.assertNotIn("location", result.values["events"][1])
        args, kwargs = post.call_args
        self.assertEqual(args, ("https://api.ship24.com/public/v1/tracking/search",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer example-token"})
        self.assertEqual(kwargs["json"], {"trackingNumber": "1Z999AA10123456784", "courierCode": ["ups"]})
        self.assertGreaterEqual(kwargs["timeout"], 60)

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_each_lookup_calls_tracking_search_even_for_the_same_number(self) -> None:
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            first = self.execute({"tracking_number": "1Z999AA10123456784"})
            second = self.execute({"tracking_number": "1Z999AA10123456784"})
        self.assertIs(first.state, CapabilityResultState.SUCCEEDED)
        self.assertIs(second.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            ["https://api.ship24.com/public/v1/tracking/search"] * 2,
        )

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_courier_omitted_and_pending_is_success(self) -> None:
        pending = {"data": {"trackings": [{
            "shipment": {"statusMilestone": "pending", "delivery": {}},
            "events": [],
        }]}}
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, pending)) as post:
            result = self.execute({"tracking_number": "SHIP24_SAMPLE_PENDING_000"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["status_milestone"], "pending")
        self.assertEqual(result.values["events"], ())
        self.assertNotIn("courierCode", post.call_args.kwargs["json"])

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_event_history_order_is_preserved_and_unknown_timezone_is_not_ranked(self) -> None:
        from copy import deepcopy

        mixed = deepcopy(TRACKED)
        mixed["data"]["trackings"][0]["events"][1]["occurrenceDatetime"] = "2026-10-16"
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, mixed)):
            result = self.execute({"tracking_number": "1Z999AA10123456784"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["events"][1]["time"], "2026-10-16")
        self.assertNotIn("last_update", result.values)

    def test_missing_key_never_calls_http(self) -> None:
        with patch.dict("os.environ", {"SHIP24_API_KEY": ""}), patch("alx.providers.ship24.httpx.post") as post:
            result = self.execute({"tracking_number": "1Z999AA10123456784"})
        self.assertEqual(result.failure["code"], "not_configured")
        post.assert_not_called()

    def test_runtime_uses_loaded_environment_without_process_export(self) -> None:
        runtime = build_ship24_runtime(
            lambda: "call-2", {"SHIP24_API_KEY": "loaded-from-dotenv"}
        )
        with patch.dict("os.environ", {"SHIP24_API_KEY": ""}), patch(
            "alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)
        ) as post:
            result = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "1Z999AA10123456784"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer loaded-from-dotenv")

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_documented_failures_are_distinct(self) -> None:
        cases = (
            (401, {"errors": []}, "authentication_failed"),
            (403, {"errors": []}, "authentication_failed"),
            (429, {"errors": []}, "rate_limited"),
            (402, {"errors": []}, "tracking_allowance_exhausted"),
            (403, {"errors": [{"code": "subscription_required"}]}, "tracking_allowance_exhausted"),
            (429, {"errors": [{"code": "subscription_limit_exceeded"}]}, "tracking_allowance_exhausted"),
            (404, {"errors": [{"code": "parcel_not_found"}]}, "tracking_not_found"),
            (422, {"errors": [{"code": "validation_error"}]}, "provider_failed"),
            (500, {"errors": [{"code": "processing_error"}]}, "provider_failed"),
        )
        for status, body, expected in cases:
            runtime = build_ship24_runtime(lambda: "call-1", {"SHIP24_API_KEY": "example-token"})
            with self.subTest(status=status), patch("alx.providers.ship24.httpx.post", return_value=response(status, body)):
                result = runtime.executors[TRACK_SHIPMENT]({"tracking_number": "1Z999AA10123456784"})
                self.assertEqual(result.failure["code"], expected)

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_network_error_is_distinct(self) -> None:
        request = httpx.Request("POST", "https://api.ship24.com/public/v1/tracking/search")
        with patch("alx.providers.ship24.httpx.post", side_effect=httpx.ConnectError("offline", request=request)):
            result = self.execute({"tracking_number": "1Z999AA10123456784"})
        self.assertEqual(result.failure["code"], "connection_failed")

    @patch.dict("os.environ", {"SHIP24_API_KEY": "example-token"})
    def test_malformed_success_and_ambiguous_trackings_fail(self) -> None:
        for body, expected in (({"data": {}}, "provider_failed"),
                               ({"data": {"trackings": [TRACKED["data"]["trackings"][0]] * 2}}, "tracking_ambiguous")):
            with self.subTest(expected=expected), patch("alx.providers.ship24.httpx.post", return_value=response(200, body)):
                result = self.execute({"tracking_number": "1Z999AA10123456784"})
                self.assertEqual(result.failure["code"], expected)


if __name__ == "__main__":
    unittest.main()
