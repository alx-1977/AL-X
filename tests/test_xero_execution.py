"""The deterministic Xero bill execution capability.

Law 2: one reusable outcome, containing whatever mechanical steps it needs.
Law 3: anything without a single objectively correct outcome returns to AL/X.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState, MailAttachment  # noqa: E402
from alx.tools import EXECUTE_XERO_BILL, build_xero_executors  # noqa: E402


PDF = b"invoice-pdf-bytes"
DIGEST = hashlib.sha256(PDF).hexdigest()


class FakeXero:
    """Records every effect so a test can prove what was and was not done."""

    def __init__(self) -> None:
        self.bills: dict[str, dict] = {}
        self.attachments: dict[str, list[tuple[str, bytes]]] = {}
        self.created = 0
        self.attach_calls = 0
        self.accounts = ({"Code": "429", "Status": "ACTIVE", "TaxType": "INPUT3"},)
        self.tax_rates = ({"TaxType": "INPUT3", "Status": "ACTIVE"},)

    def list_accounts(self):
        return self.accounts

    def list_tax_rates(self):
        return self.tax_rates

    def search_contacts(self, _term):
        return ()

    def find_bill(self, invoice_number, contact_id=""):
        for bill in self.bills.values():
            if bill["InvoiceNumber"] == invoice_number and str(
                bill["Contact"]["ContactID"]
            ) == (contact_id or bill["Contact"]["ContactID"]):
                if bill.get("Status") not in ("DELETED", "VOIDED"):
                    return bill
        return None

    def read_bill(self, invoice_id):
        return self.bills.get(invoice_id)

    def create_draft_bill(self, bill):
        self.created += 1
        invoice_id = f"bill-{self.created}"
        total = sum(
            float(item["Quantity"]) * float(item["UnitAmount"])
            + float(item.get("TaxAmount") or 0)
            for item in bill["LineItems"]
        )
        stored = {
            **bill,
            "InvoiceID": invoice_id,
            "Total": f"{total:.2f}",
            "AmountDue": f"{total:.2f}",
            "Contact": {**bill["Contact"], "Name": "Supplier"},
            "HasAttachments": False,
        }
        self.bills[invoice_id] = stored
        self.attachments[invoice_id] = []
        return stored

    def attach_bill_document(self, invoice_id, filename, _media_type, content):
        self.attach_calls += 1
        self.attachments[invoice_id].append((filename, content))
        self.bills[invoice_id]["HasAttachments"] = True
        return {"FileName": filename}

    def list_bill_attachments(self, invoice_id):
        return tuple(
            {
                "AttachmentID": f"att-{index}",
                "FileName": name,
                "MimeType": "application/pdf",
            }
            for index, (name, _content) in enumerate(self.attachments.get(invoice_id, []))
        )

    def read_bill_attachment(self, invoice_id, attachment_id, _media_type):
        index = int(attachment_id.split("-")[1])
        return self.attachments[invoice_id][index][1]

    def authorise_bill(self, invoice_id):
        self.bills[invoice_id]["Status"] = "AUTHORISED"
        return self.bills[invoice_id]

    def update_draft_bill(self, invoice_id, bill):
        self.bills[invoice_id] = {**self.bills[invoice_id], **bill}
        return self.bills[invoice_id]

    def delete_draft_bill(self, invoice_id):
        self.bills[invoice_id]["Status"] = "DELETED"
        return self.bills[invoice_id]


class FakeMail:
    def __init__(self, payload: bytes = PDF, filename: str = "invoice.pdf") -> None:
        self.attachment = MailAttachment(
            "4", filename, "application/pdf", len(payload),
            hashlib.sha256(payload).hexdigest(), "",
        )
        self.payload = payload

    def read_attachment(self, _reference, _attachment_id):
        return self.attachment, self.payload


def source(digest: str = DIGEST) -> dict:
    return {
        "mailbox_id": "INBOX",
        "uid_validity": "777",
        "uid": "12",
        "attachment_id": "4",
        "expected_sha256": digest,
    }


def bill_values(**overrides) -> dict:
    values = {
        "contact_id": "contact-1",
        "invoice_number": "SUP-42",
        "date": "2026-08-30",
        "due_date": "2026-09-30",
        "currency": "ZAR",
        "reference": "mail:777:12",
        "line_amount_types": "Exclusive",
        "expected_total": "620.00",
        "line_items": [
            {
                "description": "Transfer service",
                "quantity": "1",
                "unit_amount": "620.00",
                "account_code": "429",
                "tax_type": "NONE",
                "tax_amount": "0.00",
            }
        ],
        "source_documents": [source()],
        "authorise": True,
    }
    values.update(overrides)
    return values


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = FakeXero()
        self.mail = FakeMail()
        self.execute = build_xero_executors(
            self.xero, self.mail, lambda: "call-1"
        )[EXECUTE_XERO_BILL]

    def test_a_routine_bill_completes_in_one_capability_call(self) -> None:
        result = self.execute(bill_values())
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["status"], "AUTHORISED")
        self.assertEqual(result.values["bill"]["total"], "620.00")
        self.assertEqual(result.values["attached"], ("invoice.pdf",))
        self.assertEqual(
            result.values["steps"],
            (
                "validated_supplied_values",
                "created_draft",
                "attached_and_verified_documents",
                "authorised",
                "read_back",
                "verified",
            ),
        )

    def test_a_draft_only_run_does_not_authorise(self) -> None:
        result = self.execute(bill_values(authorise=False))
        self.assertTrue(result.values["completed"])
        self.assertEqual(result.values["bill"]["status"], "DRAFT")
        self.assertNotIn("authorised", result.values["steps"])


class DuplicateAndRetryTests(unittest.TestCase):
    """Retrying must never create a second bill or a duplicate attachment."""

    def setUp(self) -> None:
        self.xero = FakeXero()
        self.mail = FakeMail()
        self.execute = build_xero_executors(
            self.xero, self.mail, lambda: "call-1"
        )[EXECUTE_XERO_BILL]

    def test_retry_after_a_partial_run_resumes_the_same_draft(self) -> None:
        """A crash between create and attach must not duplicate the bill."""
        first = self.execute(bill_values(authorise=False))
        self.assertTrue(first.values["completed"])
        self.assertEqual(self.xero.created, 1)
        attachments_after_first = len(self.xero.attachments["bill-1"])

        second = self.execute(bill_values(authorise=False))
        self.assertTrue(second.values["completed"])
        self.assertEqual(self.xero.created, 1, "a retry created a second bill")
        self.assertEqual(
            len(self.xero.attachments["bill-1"]),
            attachments_after_first,
            "a retry duplicated the attachment",
        )
        self.assertIn("resumed_existing_draft", second.values["steps"])

    def test_repeated_execution_is_idempotent_across_many_attempts(self) -> None:
        for _ in range(4):
            self.execute(bill_values(authorise=False))
        self.assertEqual(self.xero.created, 1)
        self.assertEqual(len(self.xero.attachments["bill-1"]), 1)
        self.assertEqual(self.xero.attach_calls, 1)

    def test_an_authorised_duplicate_returns_rather_than_acting(self) -> None:
        self.execute(bill_values())
        again = self.execute(bill_values())
        self.assertFalse(again.values["completed"])
        self.assertEqual(again.values["returned_for"], "duplicate_bill")
        self.assertEqual(self.xero.created, 1)

    def test_an_existing_draft_with_a_different_total_returns(self) -> None:
        """Same invoice number, different money, is not mechanically resolvable."""
        self.execute(bill_values(authorise=False))
        changed = self.execute(
            bill_values(
                authorise=False,
                expected_total="700.00",
                line_items=[
                    {
                        "description": "Transfer service",
                        "quantity": "1",
                        "unit_amount": "700.00",
                        "account_code": "429",
                        "tax_type": "NONE",
                        "tax_amount": "0.00",
                    }
                ],
            )
        )
        self.assertFalse(changed.values["completed"])
        self.assertEqual(changed.values["returned_for"], "duplicate_bill")
        self.assertEqual(self.xero.created, 1)


class AccountingMeaningTests(unittest.TestCase):
    """The capability validates supplied accounting values; it never chooses them."""

    def setUp(self) -> None:
        self.xero = FakeXero()
        self.mail = FakeMail()
        self.execute = build_xero_executors(
            self.xero, self.mail, lambda: "call-1"
        )[EXECUTE_XERO_BILL]

    def test_an_unknown_account_returns_instead_of_substituting_one(self) -> None:
        values = bill_values()
        values["line_items"][0]["account_code"] = "999"
        result = self.execute(values)
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "account_or_tax_unresolved")
        self.assertEqual(self.xero.created, 0, "a bill was created with a guessed account")

    def test_an_unknown_tax_type_returns_instead_of_substituting_one(self) -> None:
        values = bill_values()
        values["line_items"][0]["tax_type"] = "NOT_A_REAL_TAX"
        result = self.execute(values)
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "account_or_tax_unresolved")
        self.assertEqual(self.xero.created, 0)

    def test_the_supplied_account_and_tax_reach_xero_unchanged(self) -> None:
        """Whatever AL/X decided is what gets posted, never a substitution."""
        self.execute(bill_values())
        line = self.xero.bills["bill-1"]["LineItems"][0]
        self.assertEqual(line["AccountCode"], "429")
        self.assertEqual(line["TaxType"], "NONE")

    def test_the_capability_never_calls_contact_search(self) -> None:
        """Choosing a supplier from candidates is AL/X's judgment, not a lookup."""
        searches = []
        self.xero.search_contacts = lambda term: searches.append(term) or ()
        self.execute(bill_values())
        self.assertEqual(searches, [])

    def test_unbalanced_lines_are_refused_before_any_write(self) -> None:
        result = self.execute(bill_values(expected_total="999.99"))
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.xero.created, 0)


class AmbiguityReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = FakeXero()
        self.mail = FakeMail()

    def _execute(self, mail=None):
        return build_xero_executors(
            self.xero, mail or self.mail, lambda: "call-1"
        )[EXECUTE_XERO_BILL]

    def test_an_attachment_hash_mismatch_returns_and_leaves_the_draft(self) -> None:
        result = self._execute()(
            bill_values(source_documents=[source(digest="0" * 64)])
        )
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "attachment_mismatch")
        # The draft exists and is returned so AL/X can decide what to do with it.
        self.assertEqual(result.values["bill"]["status"], "DRAFT")
        self.assertEqual(self.xero.attach_calls, 0)

    def test_a_read_back_mismatch_returns_rather_than_reporting_success(self) -> None:
        execute = self._execute()

        original = self.xero.read_bill

        def altered(invoice_id):
            bill = original(invoice_id)
            return None if bill is None else {**bill, "Total": "1.00"}

        self.xero.read_bill = altered
        result = execute(bill_values())
        self.assertFalse(result.values["completed"])
        self.assertEqual(result.values["returned_for"], "read_back_mismatch")

    def test_missing_source_documents_are_refused(self) -> None:
        result = self._execute()(bill_values(source_documents=[]))
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.xero.created, 0)

    def test_every_return_names_why_it_came_back(self) -> None:
        values = bill_values()
        values["line_items"][0]["account_code"] = "999"
        result = self._execute()(values)
        self.assertTrue(result.values["returned_for"])
        self.assertTrue(result.values["detail"])


if __name__ == "__main__":
    unittest.main()
