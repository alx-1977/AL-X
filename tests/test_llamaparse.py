"""LlamaCloud invoice extraction is a bounded document read, nothing more.

The adapter uploads original bytes, requests AL/X's invoice schema, and
returns fields for `checked_invoice`. It never talks to Core, never falls
back to the generic specialist, and never carries document bytes or the API
key on a failure.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.xero import build_supplier_invoice_extractor  # noqa: E402
from alx.config import LlamaParseSettings  # noqa: E402
from alx.contracts import SpecialistError  # noqa: E402
from alx.providers.llamaparse import (  # noqa: E402
    EXTRACT_PATH,
    EXTRACT_TARGET,
    EXTRACT_TIER,
    FILES_PATH,
    LlamaParseInvoiceExtractor,
    MAX_DOCUMENT_BYTES,
)
from alx.specialists import ANSWER_SCHEMA, INSTRUCTION, checked_invoice  # noqa: E402


API_KEY = "llamacloud-secret"
INVOICE_BYTES = b"%PDF-scanned-invoice-bytes"
INVOICE_TEXT = "SAMTEC INC Invoice 18300777 Total USD 180.00"
FIELDS = {
    "document_type": "supplier_invoice",
    "supplier_name": "SAMTEC",
    "invoice_number": "18300777",
    "invoice_date": "2026-08-20",
    "due_date": "2026-09-20",
    "currency": "USD",
    "subtotal": "180.00",
    "tax_amount": "0.00",
    "total": "180.00",
    "description": "Electronic components",
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LlamaCloud:
    def __init__(self, poll_bodies=None, upload_status=200, extract_status=200) -> None:
        self.uploads: list[httpx.Request] = []
        self.extracts: list[httpx.Request] = []
        self.polls: list[httpx.Request] = []
        self.upload_status = upload_status
        self.extract_status = extract_status
        self.poll_bodies = list(
            poll_bodies
            or (
                {
                    "id": "ext-1",
                    "status": "COMPLETED",
                    "extract_result": dict(FIELDS),
                },
            )
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith(FILES_PATH):
            self.uploads.append(request)
            if self.upload_status != 200:
                return httpx.Response(
                    self.upload_status,
                    json={"error": f"{INVOICE_TEXT} key={API_KEY}"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "dfl-1",
                    "name": "invoice.pdf",
                    "project_id": "prj-from-upload",
                    "purpose": "extract",
                },
            )
        if request.method == "POST" and path.endswith(EXTRACT_PATH):
            self.extracts.append(request)
            if self.extract_status != 200:
                return httpx.Response(
                    self.extract_status,
                    json={"error": f"{INVOICE_TEXT} key={API_KEY}"},
                )
            return httpx.Response(200, json={"id": "ext-1", "status": "PENDING"})
        if request.method == "GET" and EXTRACT_PATH in path:
            self.polls.append(request)
            body = self.poll_bodies.pop(0) if self.poll_bodies else {
                "id": "ext-1",
                "status": "PENDING",
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unknown"})


def extractor(cloud: LlamaCloud, **changes) -> LlamaParseInvoiceExtractor:
    clock = changes.pop("clock", Clock())
    sleeper = changes.pop("sleeper", lambda seconds: clock.advance(seconds))
    return LlamaParseInvoiceExtractor(
        API_KEY,
        "https://api.cloud.llamaindex.ai",
        changes.pop("timeout_seconds", 60),
        changes.pop("project_id", ""),
        ANSWER_SCHEMA,
        INSTRUCTION,
        httpx.Client(transport=httpx.MockTransport(cloud)),
        sleeper,
        clock,
        changes.pop("poll_interval_seconds", 1.0),
    )


class LlamaParseSettingsTests(unittest.TestCase):
    def test_empty_configuration_is_not_usable(self) -> None:
        settings = LlamaParseSettings.from_environment({})
        self.assertFalse(settings.is_usable)
        self.assertEqual(settings.api_key, "")
        self.assertIsNone(build_supplier_invoice_extractor(settings))

    def test_preferred_key_does_not_inherit_core_credentials(self) -> None:
        settings = LlamaParseSettings.from_environment(
            {
                "OPENAI_API_KEY": "core-secret",
                "ALX_REASONING_API_KEY": "core-secret",
                "ALX_SPECIALIST_API_KEY": "specialist-secret",
            }
        )
        self.assertFalse(settings.is_usable)
        self.assertEqual(settings.api_key, "")

    def test_documented_alias_is_accepted(self) -> None:
        settings = LlamaParseSettings.from_environment(
            {"LLAMA_CLOUD_API_KEY": "alias-secret"}
        )
        self.assertTrue(settings.is_usable)
        self.assertEqual(settings.api_key, "alias-secret")

    def test_preferred_key_wins_over_the_alias(self) -> None:
        settings = LlamaParseSettings.from_environment(
            {
                "ALX_LLAMAPARSE_API_KEY": "preferred",
                "LLAMA_CLOUD_API_KEY": "alias-secret",
            }
        )
        self.assertEqual(settings.api_key, "preferred")

    def test_repr_redacts_the_credential(self) -> None:
        settings = LlamaParseSettings.from_environment(
            {"ALX_LLAMAPARSE_API_KEY": "visible-secret"}
        )
        self.assertNotIn("visible-secret", repr(settings))
        self.assertIn("<redacted>", repr(settings))


class LlamaParseAdapterTests(unittest.TestCase):
    def test_original_bytes_are_uploaded_to_the_documented_files_endpoint(self) -> None:
        cloud = LlamaCloud()
        result = extractor(cloud).extract(
            INVOICE_BYTES,
            "application/pdf",
            "Invoice 18300777.pdf",
            "Invoice 18300777.pdf",
        )
        self.assertEqual(len(cloud.uploads), 1)
        upload = cloud.uploads[0]
        self.assertEqual(upload.url.path, FILES_PATH)
        self.assertEqual(upload.headers["Authorization"], f"Bearer {API_KEY}")
        self.assertIn(INVOICE_BYTES, upload.content)
        self.assertIn(b"purpose", upload.content)
        self.assertIn(b"extract", upload.content)
        self.assertEqual(result["invoice_number"], "18300777")
        self.assertEqual(result["total"], "180.00")

    def test_extract_requests_alx_schema_not_a_stock_invoice_schema(self) -> None:
        cloud = LlamaCloud()
        extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        sent = json.loads(cloud.extracts[0].content)
        self.assertEqual(cloud.extracts[0].url.path, EXTRACT_PATH)
        self.assertEqual(sent["file_input"], "dfl-1")
        configuration = sent["configuration"]
        self.assertEqual(configuration["tier"], EXTRACT_TIER)
        self.assertEqual(configuration["extraction_target"], EXTRACT_TARGET)
        self.assertEqual(configuration["data_schema"], ANSWER_SCHEMA)
        self.assertEqual(configuration["system_prompt"], INSTRUCTION)
        self.assertNotIn("structured_output_json_schema_name", sent)
        self.assertNotIn("structured_output_json_schema_name", configuration)

    def test_context_line_is_offered_without_becoming_the_document(self) -> None:
        cloud = LlamaCloud()
        extractor(cloud).extract(
            INVOICE_BYTES, "application/pdf", "invoice.pdf", "Invoice 18300777.pdf"
        )
        prompt = json.loads(cloud.extracts[0].content)["configuration"]["system_prompt"]
        self.assertIn("Invoice 18300777.pdf", prompt)
        self.assertIn(INSTRUCTION.splitlines()[0], prompt)
        self.assertNotIn(INVOICE_BYTES.decode("latin-1"), prompt)

    def test_upload_project_id_is_used_when_none_is_configured(self) -> None:
        cloud = LlamaCloud()
        extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        self.assertEqual(
            cloud.extracts[0].url.params.get("project_id"), "prj-from-upload"
        )
        self.assertEqual(
            cloud.polls[0].url.params.get("project_id"), "prj-from-upload"
        )

    def test_configured_project_id_is_sent_on_every_call(self) -> None:
        cloud = LlamaCloud()
        extractor(cloud, project_id="prj-configured").extract(
            INVOICE_BYTES, "application/pdf", "invoice.pdf"
        )
        self.assertEqual(cloud.uploads[0].url.params.get("project_id"), "prj-configured")
        self.assertEqual(cloud.extracts[0].url.params.get("project_id"), "prj-configured")

    def test_checked_invoice_accepts_the_structured_response(self) -> None:
        cloud = LlamaCloud()
        values = extractor(cloud).extract(
            INVOICE_BYTES, "application/pdf", "invoice.pdf"
        )
        checked = checked_invoice(values)
        self.assertTrue(checked["verified"])
        self.assertEqual(checked["problems"], ())
        self.assertEqual(checked["invoice_number"], "18300777")
        self.assertEqual(checked["total"], "180.00")

    def test_numeric_amounts_are_coerced_for_checked_invoice(self) -> None:
        fields = dict(FIELDS, subtotal=180.0, tax_amount=0, total=180.0)
        cloud = LlamaCloud(
            poll_bodies=({"id": "ext-1", "status": "COMPLETED", "extract_result": fields},)
        )
        checked = checked_invoice(
            extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        )
        self.assertTrue(checked["verified"])
        self.assertEqual(checked["total"], "180.0")

    def test_incomplete_extraction_fails_closed_through_checked_invoice(self) -> None:
        fields = dict(FIELDS, invoice_number="", total="")
        cloud = LlamaCloud(
            poll_bodies=({"id": "ext-1", "status": "COMPLETED", "extract_result": fields},)
        )
        checked = checked_invoice(
            extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        )
        self.assertFalse(checked["verified"])
        self.assertIn("invoice number missing", checked["problems"])
        self.assertIn("total missing or unreadable", checked["problems"])

    def test_a_non_object_result_is_not_structured(self) -> None:
        cloud = LlamaCloud(
            poll_bodies=({"id": "ext-1", "status": "COMPLETED", "extract_result": [FIELDS]},)
        )
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        self.assertEqual(captured.exception.code, "answer_not_structured")

    def test_empty_bytes_never_reach_llamacloud(self) -> None:
        cloud = LlamaCloud()
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(b"", "application/pdf", "invoice.pdf")
        self.assertEqual(captured.exception.code, "document_has_no_text")
        self.assertEqual(cloud.uploads, [])

    def test_an_oversized_document_never_reaches_llamacloud(self) -> None:
        cloud = LlamaCloud()
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(
                b"x" * (MAX_DOCUMENT_BYTES + 1), "application/pdf", "invoice.pdf"
            )
        self.assertEqual(captured.exception.code, "document_too_large")
        self.assertEqual(cloud.uploads, [])

    def test_unsupported_media_never_reaches_llamacloud(self) -> None:
        cloud = LlamaCloud()
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(INVOICE_BYTES, "application/zip", "archive.zip")
        self.assertEqual(captured.exception.code, "unsupported_media_type")
        self.assertEqual(cloud.uploads, [])

    def test_a_pathological_filename_is_reduced_to_its_name(self) -> None:
        cloud = LlamaCloud()
        extractor(cloud).extract(
            INVOICE_BYTES, "application/octet-stream", "../../Invoice 18300777.pdf"
        )
        self.assertIn(b"Invoice 18300777.pdf", cloud.uploads[0].content)
        self.assertNotIn(b"../", cloud.uploads[0].content)

    def test_timeout_is_sanitised(self) -> None:
        cloud = LlamaCloud(
            poll_bodies=(
                {"id": "ext-1", "status": "PENDING"},
                {"id": "ext-1", "status": "PENDING"},
            )
        )
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud, timeout_seconds=1).extract(
                INVOICE_BYTES, "application/pdf", "invoice.pdf"
            )
        self.assertEqual(captured.exception.code, "extraction_timeout")
        self.assertNotIn(INVOICE_TEXT, str(captured.exception))
        self.assertNotIn(API_KEY, str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)

    def test_http_failures_are_sanitised(self) -> None:
        cloud = LlamaCloud(upload_status=500)
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        self.assertEqual(captured.exception.code, "provider_failed")
        self.assertNotIn(INVOICE_TEXT, str(captured.exception))
        self.assertNotIn(API_KEY, str(captured.exception))
        self.assertNotIn(INVOICE_BYTES.decode("latin-1"), str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)

    def test_a_failed_job_does_not_carry_the_provider_error_message(self) -> None:
        cloud = LlamaCloud(
            poll_bodies=(
                {
                    "id": "ext-1",
                    "status": "FAILED",
                    "error_message": f"could not read {INVOICE_TEXT}",
                },
            )
        )
        with self.assertRaises(SpecialistError) as captured:
            extractor(cloud).extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        self.assertEqual(captured.exception.code, "provider_failed")
        self.assertNotIn("SAMTEC", str(captured.exception))
        self.assertNotIn(INVOICE_TEXT, str(captured.exception))

    def test_transport_errors_are_sanitised(self) -> None:
        def fail(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connecting with {API_KEY} for {INVOICE_TEXT}")

        adapter = LlamaParseInvoiceExtractor(
            API_KEY,
            "https://api.cloud.llamaindex.ai",
            60,
            "",
            ANSWER_SCHEMA,
            INSTRUCTION,
            httpx.Client(transport=httpx.MockTransport(fail)),
        )
        with self.assertRaises(SpecialistError) as captured:
            adapter.extract(INVOICE_BYTES, "application/pdf", "invoice.pdf")
        self.assertEqual(captured.exception.code, "provider_failed")
        self.assertNotIn(API_KEY, str(captured.exception))
        self.assertNotIn(INVOICE_TEXT, str(captured.exception))
        self.assertIsNone(captured.exception.__context__)

    def test_wired_extractor_returns_checked_invoice(self) -> None:
        cloud = LlamaCloud()
        wired = build_supplier_invoice_extractor(
            LlamaParseSettings(
                api_key=API_KEY,
                base_url="https://api.cloud.llamaindex.ai",
                timeout_seconds=60,
                project_id="",
            ),
            client=httpx.Client(transport=httpx.MockTransport(cloud)),
        )
        self.assertIsNotNone(wired)
        result = wired(INVOICE_BYTES, "application/pdf", "invoice.pdf", "")
        self.assertTrue(result["verified"])
        self.assertEqual(result["invoice_number"], "18300777")
        self.assertIn("problems", result)


if __name__ == "__main__":
    unittest.main()
