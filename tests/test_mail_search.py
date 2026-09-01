from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityResultState,
    MailAccessError,
    MailSearchCriteria,
)
from alx.providers import ICloudMailAdapter, SQLiteMailObservationState  # noqa: E402
from alx.tools import SEARCH_MAIL_MESSAGES, build_mail_executors  # noqa: E402


def message(subject: str, *, attachment: bool = False) -> bytes:
    if not attachment:
        return (
            f"Subject: {subject}\r\nFrom: Supplier <accounts@example.test>\r\n"
            "Date: Sun, 30 Aug 2026 10:00:00 +0200\r\n\r\nInvoice follows."
        ).encode()
    return (
        f"Subject: {subject}\r\nFrom: Supplier <accounts@example.test>\r\n"
        "Date: Sun, 30 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
        "--x\r\nContent-Type: text/plain\r\n\r\nInvoice\r\n"
        "--x\r\nContent-Type: application/pdf\r\n"
        "Content-Disposition: attachment; filename=invoice.pdf\r\n\r\npdf\r\n--x--\r\n"
    ).encode()


class SearchImap:
    def __init__(self) -> None:
        self.readonly = None
        self.search_values = None
        self.fetch_values = []
        self.messages = {"7": message("Old invoice"), "9": message("New invoice", attachment=True)}

    def login(self, _address, _secret): return "OK", []
    def logout(self): return "BYE", []
    def select(self, _mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]
    def response(self, _name): return "UIDVALIDITY", [b"777"]
    def uid(self, operation, *values):
        if operation == "search":
            self.search_values = values
            return "OK", [b"7 9"]
        if operation == "fetch":
            self.fetch_values.append(values)
            uid = values[0]
            flags = b"\\Seen" if uid == "7" else b""
            structure = (
                b'BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 7 1) '
                b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 3 NIL '
                b'("ATTACHMENT" ("FILENAME" "invoice.pdf"))) "MIXED")'
                if uid == "9"
                else b'BODYSTRUCTURE ("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 7 1)'
            )
            return "OK", [
                (b"FLAGS (" + flags + b") " + structure, self.messages[uid]),
                b")",
            ]
        raise AssertionError((operation, values))


class MailSearchTests(unittest.TestCase):
    def test_historical_structured_search_is_read_only_and_returns_stable_uids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteMailObservationState(Path(directory) / "state.sqlite3")
            connection = SearchImap()
            account = ICloudMailAdapter(
                "imap.example.test", 993, "friedl@example.test", "secret", state, 1,
                connection_factory=lambda *args, **kwargs: connection,
            )
            found, truncated = account.search(
                MailSearchCriteria(
                    "INBOX", sender="accounts@example.test", subject="invoice",
                    date_from="2026-08-01", date_to="2026-08-30",
                    seen_state="any", has_attachments=True, limit=10,
                )
            )
            state.close()

        self.assertTrue(connection.readonly)
        self.assertTrue(
            all(
                values[1] == "(BODY.PEEK[HEADER] BODYSTRUCTURE FLAGS)"
                for values in connection.fetch_values
            )
        )
        self.assertEqual([item.reference.uid for item in found], ["9"])
        self.assertEqual(found[0].reference.uid_validity, "777")
        self.assertTrue(found[0].has_attachments)
        self.assertFalse(found[0].seen)
        self.assertFalse(truncated)
        self.assertEqual(
            connection.search_values,
            (None, "FROM", '"accounts@example.test"', "SUBJECT", '"invoice"', "SINCE", "01-Aug-2026", "BEFORE", "31-Aug-2026"),
        )

    def test_search_contract_rejects_imap_control_characters(self) -> None:
        with self.assertRaises(ValueError):
            MailSearchCriteria("INBOX", sender="supplier\r\nSEEN")

    def test_bodystructure_attachment_detection_does_not_need_payloads(self) -> None:
        detect = ICloudMailAdapter._bodystructure_has_attachments
        plain_alternative = [
            b'BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 7 1) '
            b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1) "ALTERNATIVE")'
        ]
        inline_without_filename = [
            b'BODYSTRUCTURE ("IMAGE" "PNG" NIL "logo" NIL "BASE64" 10 NIL '
            b'("INLINE" NIL))'
        ]
        inline_with_filename = [
            b'BODYSTRUCTURE ("IMAGE" "PNG" ("NAME" "logo.png") "logo" NIL '
            b'"BASE64" 10 NIL ("INLINE" ("FILENAME" "logo.png")))'
        ]
        self.assertFalse(detect(plain_alternative))
        self.assertFalse(detect(inline_without_filename))
        self.assertTrue(detect(inline_with_filename))
        with self.assertRaises(MailAccessError) as captured:
            detect([b"FLAGS ()"])
        self.assertEqual(captured.exception.code, "search_failed")

    def test_search_tool_carries_mail_provenance_without_observation_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteMailObservationState(Path(directory) / "state.sqlite3")
            connection = SearchImap()
            account = ICloudMailAdapter(
                "imap.example.test", 993, "friedl@example.test", "secret", state, 1,
                connection_factory=lambda *args, **kwargs: connection,
            )
            executor = build_mail_executors(
                account, state, lambda: "search-1",
                clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
            )[SEARCH_MAIL_MESSAGES]
            result = executor({"mailbox_id": "INBOX", "limit": 1})
            state.close()

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["messages"][0]["reference"]["uid"], "9")
        self.assertTrue(result.values["truncated"])
        self.assertEqual(
            {(item.uid_validity, item.uid) for item in result.provenance.mail_references},
            {("777", "9")},
        )


if __name__ == "__main__":
    unittest.main()
