from __future__ import annotations

import tempfile
import unittest
import io
import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState, MailAccessError, MailReference  # noqa: E402
from alx.providers import ICloudMailAdapter, SQLiteMailObservationState  # noqa: E402
from alx.tools import (  # noqa: E402
    LIST_MAIL_ATTACHMENTS,
    READ_MAIL_ATTACHMENT,
    build_mail_executors,
)
from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject  # noqa: E402


def invoice_message() -> bytes:
    return (
        "Message-ID: <invoice@example.test>\r\n"
        "Subject: Supplier invoice\r\n"
        "From: Supplier <accounts@example.test>\r\n"
        "Date: Sun, 30 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        "--part\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Please find our invoice attached.\r\n"
        "--part\r\nContent-Type: text/csv; charset=utf-8\r\n"
        "Content-Disposition: attachment; filename=invoice.csv\r\n\r\n"
        "invoice,total\r\nSUP-42,1250.00\r\n"
        "--part--\r\n"
    ).encode()


def zip_message() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nested/MyBill_gdb.csv", "invoice,total\nDHL-1,100.00\n")
        archive.writestr("nested/SAD_500.pdf", b"%PDF-not-text")
    import base64

    encoded = base64.encodebytes(buffer.getvalue()).decode("ascii")
    return (
        "Message-ID: <dhl@example.test>\r\n"
        "Subject: DHL documents\r\n"
        "From: DHL <documents@example.test>\r\n"
        "Date: Sun, 30 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        "--part\r\nContent-Type: text/plain\r\n\r\nDocuments\r\n"
        "--part\r\nContent-Type: application/zip\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "Content-Disposition: attachment; filename=customs.zip\r\n\r\n"
        f"{encoded}\r\n--part--\r\n"
    ).encode()


def nested_zip_message() -> bytes:
    worksheet = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(worksheet)
    sad = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(sad)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("docs/worksheet.pdf", worksheet.getvalue())
        archive.writestr("docs/SAD_500.pdf", sad.getvalue())
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("shipment/customs-documents.zip", inner.getvalue())
    import base64

    encoded = base64.encodebytes(outer.getvalue()).decode("ascii")
    return (
        "Message-ID: <nested-dhl@example.test>\r\n"
        "Subject: Nested DHL documents\r\n"
        "From: DHL <documents@example.test>\r\n"
        "Date: Sun, 30 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        "--part\r\nContent-Type: application/zip\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "Content-Disposition: attachment; filename=delivery.zip\r\n\r\n"
        f"{encoded}\r\n--part--\r\n"
    ).encode()


class FakeImap:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def login(self, _address, _secret):
        return "OK", []

    def logout(self):
        return "BYE", []

    def select(self, _mailbox, readonly=False):
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"777"]

    def uid(self, operation, *values):
        if operation == "fetch":
            return "OK", [(b"metadata", self.raw), b")"]
        raise AssertionError((operation, values))


class MailAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        state = SQLiteMailObservationState(
            Path(self.directory.name) / "observations.sqlite3"
        )
        self.addCleanup(state.close)
        self.account = ICloudMailAdapter(
            "imap.example.test",
            993,
            "friedl@example.test",
            "secret",
            state,
            1,
            connection_factory=lambda *args, **kwargs: FakeImap(invoice_message()),
        )
        self.reference = MailReference("INBOX", "777", "1")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_provider_lists_only_the_real_attachment(self) -> None:
        attachments = self.account.list_attachments(self.reference)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].filename, "invoice.csv")
        self.assertEqual(attachments[0].media_type, "text/csv")
        self.assertTrue(attachments[0].sha256)
        self.assertEqual(attachments[0].text, "")

    def test_provider_reads_exact_attachment_and_extracts_text(self) -> None:
        listed = self.account.list_attachments(self.reference)[0]
        attachment, payload = self.account.read_attachment(
            self.reference, listed.attachment_id
        )
        self.assertEqual(attachment.sha256, listed.sha256)
        self.assertEqual(payload, b"invoice,total\r\nSUP-42,1250.00")
        self.assertIn("SUP-42,1250.00", attachment.text)

    def test_attachment_text_is_transient_but_metadata_is_durable(self) -> None:
        calls = iter(("list", "read"))
        executors = build_mail_executors(
            self.account,
            observations=type("Observation", (), {"acknowledge": lambda *_: None})(),
            call_id_source=lambda: next(calls),
            clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
        )
        arguments = {
            "mailbox_id": "INBOX",
            "uid_validity": "777",
            "uid": "1",
        }
        listed = executors[LIST_MAIL_ATTACHMENTS](arguments)
        self.assertEqual(listed.state, CapabilityResultState.SUCCEEDED)
        attachment_id = listed.values["attachments"][0]["attachment_id"]
        read = executors[READ_MAIL_ATTACHMENT](
            {**arguments, "attachment_id": attachment_id}
        )
        self.assertIn("SUP-42", read.values["text"])
        self.assertNotIn("text", read.durable_values)
        self.assertIsNotNone(read.provenance)

    def test_missing_attachment_identifier_fails_without_returning_content(self) -> None:
        executors = build_mail_executors(
            self.account,
            observations=type("Observation", (), {"acknowledge": lambda *_: None})(),
            call_id_source=lambda: "missing",
        )
        result = executors[READ_MAIL_ATTACHMENT](
            {
                "mailbox_id": "INBOX",
                "uid_validity": "777",
                "uid": "1",
                "attachment_id": "999",
            }
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "attachment_unavailable")

    def test_zip_members_are_exact_virtual_attachments(self) -> None:
        state = SQLiteMailObservationState(
            Path(self.directory.name) / "zip-observations.sqlite3"
        )
        self.addCleanup(state.close)
        account = ICloudMailAdapter(
            "imap.example.test",
            993,
            "friedl@example.test",
            "secret",
            state,
            1,
            connection_factory=lambda *args, **kwargs: FakeImap(zip_message()),
        )
        attachments = account.list_attachments(self.reference)
        self.assertEqual(
            [item.filename for item in attachments],
            ["customs.zip", "MyBill_gdb.csv", "SAD_500.pdf"],
        )
        csv_attachment = attachments[1]
        self.assertIn(":", csv_attachment.attachment_id)
        reread, payload = account.read_attachment(
            self.reference, csv_attachment.attachment_id
        )
        self.assertEqual(reread.sha256, csv_attachment.sha256)
        self.assertEqual(payload, b"invoice,total\nDHL-1,100.00\n")

    def test_nested_zip_members_are_reachable_by_stable_identifiers(self) -> None:
        state = SQLiteMailObservationState(
            Path(self.directory.name) / "nested-observations.sqlite3"
        )
        self.addCleanup(state.close)
        account = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret", state, 1,
            connection_factory=lambda *args, **kwargs: FakeImap(nested_zip_message()),
        )
        attachments = account.list_attachments(self.reference)
        self.assertEqual(
            [item.filename for item in attachments],
            ["delivery.zip", "customs-documents.zip", "worksheet.pdf", "SAD_500.pdf"],
        )
        worksheet = attachments[2]
        self.assertEqual(worksheet.attachment_id.count(":"), 2)
        reread, payload = account.read_attachment(self.reference, worksheet.attachment_id)
        self.assertEqual(reread.sha256, worksheet.sha256)
        self.assertTrue(payload.startswith(b"%PDF"))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), worksheet.sha256)

    def test_archive_over_member_limit_is_refused_instead_of_undercounted(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            for index in range(101):
                bundle.writestr(f"document-{index}.txt", "x")
        import base64

        encoded = base64.encodebytes(archive.getvalue()).decode("ascii")
        raw = (
            "Subject: Too many documents\r\nMIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=x\r\n\r\n"
            "--x\r\nContent-Type: application/zip\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "Content-Disposition: attachment; filename=documents.zip\r\n\r\n"
            f"{encoded}\r\n--x--\r\n"
        ).encode()
        state = SQLiteMailObservationState(
            Path(self.directory.name) / "unsafe-observations.sqlite3"
        )
        self.addCleanup(state.close)
        account = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret", state, 1,
            connection_factory=lambda *args, **kwargs: FakeImap(raw),
        )
        with self.assertRaises(MailAccessError) as captured:
            account.list_attachments(self.reference)
        self.assertEqual(captured.exception.code, "archive_unsafe")

    def test_pdf_invoice_text_is_extracted_only_when_the_attachment_is_read(self) -> None:
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            b"BT /F1 10 Tf 1 0 0 1 100 700 Tm (Supplier Invoice SUP-42 Total R1250.00) Tj ET"
        )
        page[NameObject("/Contents")] = stream
        output = io.BytesIO()
        writer.write(output)

        from alx.providers.icloud_mail import _attachment_text

        text = _attachment_text("application/pdf", output.getvalue(), None)
        self.assertIn("Supplier Invoice SUP-42", text)
        self.assertIn("R1250.00", text)


if __name__ == "__main__":
    unittest.main()
