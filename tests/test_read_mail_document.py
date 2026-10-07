"""D-038: AL/X reads what a mailed commercial document states.

On 2026-10-06 she could not read BlueNova's purchase order and matched it to
a quote by the attachment's filename. These tests prove the document is read
against the fixed schema, bounded, bound to the exact attachment, and returned
as mail content.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.documents import DOCUMENT_READ_PERMISSION, build_document_runtime  # noqa: E402
from alx.config import LlamaParseSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityResultState,
    ContentOrigin,
    MailAttachment,
    SideEffect,
    SpecialistError,
)
from alx.specialists.commercial_document import DOCUMENT_SCHEMA  # noqa: E402
from alx.tools.documents import (  # noqa: E402
    DEFINITION,
    READ_MAIL_DOCUMENT,
    build_document_executors,
)

PDF = b"%PDF-po"
DIGEST = hashlib.sha256(PDF).hexdigest()
PO = {
    "document_type": "purchase_order", "document_number": "PO-09860",
    "issued_by": "BlueNova Energy (Pty) Ltd", "issued_to": "FireFli",
    "date": "2026-10-05", "currency": "ZAR", "references": ["QU-0136"],
    "subtotal": "14528.00", "tax_amount": "2179.20", "total": "16707.20",
    "lines": [{"description": "HV measurement boards", "quantity": "100",
               "unit_price": "145.28", "amount": "14528.00"}],
}


class FakeMail:
    def __init__(self) -> None:
        self.read = []

    def read_attachment(self, reference, attachment_id):
        self.read.append((reference, attachment_id))
        return MailAttachment(attachment_id, "PO-09860.pdf", "application/pdf",
                              len(PDF), DIGEST, ""), PDF


def arguments(**changes) -> dict:
    values = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "60424",
              "attachment_id": "5", "expected_sha256": DIGEST}
    values.update(changes)
    return values


class ReadMailDocumentTests(unittest.TestCase):
    def execute(self, reader, **changes):
        return build_document_executors(
            FakeMail(), reader, lambda: "call-1",
            clock=lambda: datetime(2026, 10, 7, tzinfo=UTC),
        )[READ_MAIL_DOCUMENT](arguments(**changes))

    def test_a_purchase_order_is_read_into_its_fields(self) -> None:
        sent = []
        result = self.execute(lambda payload, media, name: sent.append((payload, media, name)) or PO)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        document = result.values["document"]
        self.assertEqual(document["total"], "16707.20")
        self.assertEqual(document["references"], ("QU-0136",))
        self.assertEqual(document["lines"][0]["quantity"], "100")
        self.assertEqual(document["lines"][0]["unit_price"], "145.28")
        self.assertEqual(sent, [(PDF, "application/pdf", "PO-09860.pdf")])
        self.assertTrue(DEFINITION.output_schema.accepts(result.values))

    def test_what_a_document_says_is_mail_content(self) -> None:
        result = self.execute(lambda *_: PO)
        self.assertIn(ContentOrigin.MAIL_MESSAGE, result.provenance.origins)
        self.assertIsNotNone(result.provenance.content_expires_at)

    def test_a_different_file_is_never_read(self) -> None:
        read = []
        result = self.execute(lambda *a: read.append(a) or PO, expected_sha256="0" * 64)
        self.assertEqual(result.failure["code"], "source_mismatch")
        self.assertEqual(read, [])

    def test_only_the_schema_s_fields_come_back_bounded(self) -> None:
        noisy = {**PO, "secret": "x", "issued_by": "y" * 5000,
                 "lines": [{"description": "a", "quantity": 1, "unit_price": 2.5,
                            "amount": 2.5, "extra": "z"}] * 300}
        document = self.execute(lambda *_: noisy).values["document"]
        self.assertNotIn("secret", document)
        self.assertEqual(len(document["issued_by"]), 2000)
        self.assertEqual(len(document["lines"]), 200)
        self.assertEqual(document["lines"][0],
                         {"description": "a", "quantity": "1", "unit_price": "2.5", "amount": "2.5"})

    def test_a_reader_failure_is_a_declared_failure(self) -> None:
        def failing(*_):
            raise SpecialistError("extraction_timeout")
        result = self.execute(failing)
        self.assertEqual(result.failure["code"], "extraction_timeout")
        self.assertIn("extraction_timeout", DEFINITION.possible_failure_codes)

    def test_it_reads_only(self) -> None:
        self.assertIs(DEFINITION.side_effect, SideEffect.NONE)


class DocumentRuntimeTests(unittest.TestCase):
    def test_it_is_absent_without_llamaparse(self) -> None:
        settings = LlamaParseSettings.from_environment({})
        self.assertIsNone(build_document_runtime(settings, FakeMail(), lambda: "c"))

    def test_it_has_its_own_permission_and_schema(self) -> None:
        settings = LlamaParseSettings.from_environment({"ALX_LLAMAPARSE_API_KEY": "key"})
        runtime = build_document_runtime(settings, FakeMail(), lambda: "c")
        self.assertIsNotNone(runtime)
        self.assertEqual(
            runtime.policies[READ_MAIL_DOCUMENT].permission_references,
            frozenset({DOCUMENT_READ_PERMISSION}),
        )
        self.assertFalse(runtime.policies[READ_MAIL_DOCUMENT].approval_required)
        self.assertIn("lines", DOCUMENT_SCHEMA["required"])


if __name__ == "__main__":
    unittest.main()
