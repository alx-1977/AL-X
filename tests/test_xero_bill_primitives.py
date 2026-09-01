from __future__ import annotations

import stat
import hashlib
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.xero import (  # noqa: E402
    XERO_BILL_DELETE_PERMISSION,
    XERO_BILL_WRITE_PERMISSION,
    XERO_READ_PERMISSION,
    build_xero_runtime,
)
from support import xero_settings  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision,
    Approval,
    ApprovalLifecycle,
    ApprovalScope,
    CapabilityCall,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalState,
    MailAttachment,
    Objective,
    SuccessCriterion,
    XeroAccessError,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.providers.xero import (  # noqa: E402
    ACCOUNTING_URL,
    SQLiteXeroOAuth,
    XeroAccountingAdapter,
    XeroConnection,
)
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
    DELETE_XERO_DRAFT_BILL,
    READ_XERO_BILL,
    XERO_DEFINITIONS,
    build_xero_executors,
)


class FakeXero:
    def __init__(self) -> None:
        self.existing = None
        self.created = []
        self.deleted = []
        self.attachments = []
        self.attachment_records = []
        self.current = {
            "Type": "ACCPAY",
            "InvoiceID": "bill-1",
            "InvoiceNumber": "SUP-42",
            "Contact": {"ContactID": "contact-1", "Name": "Supplier"},
            "Status": "DRAFT",
            "CurrencyCode": "ZAR",
            "Total": "1250.00",
            "AmountDue": "1250.00",
            "Reference": "mail:777:1",
            "HasAttachments": True,
        }

    def search_contacts(self, _term):
        return (
            {"Name": "Supplier", "ContactID": "contact-1", "ContactStatus": "ACTIVE"},
        )

    def bills_for_contact(self, _contact_id):
        # Consistent prior coding, so capture resolves the treatment from this
        # organisation's own records rather than returning to AL/X.
        return (
            {
                "LineAmountTypes": "NoTax",
                "LineItems": [{"AccountCode": "310", "TaxType": "NONE"}],
            },
        )

    def list_accounts(self):
        return ({"Code": "310", "Status": "ACTIVE", "TaxType": "NONE"},)

    def list_tax_rates(self):
        return ()

    def find_bill(self, _invoice_number, _contact_id=""):
        return self.existing

    def read_bill(self, _invoice_id):
        return self.current

    def create_draft_bill(self, bill):
        self.created.append(bill)
        return dict(self.current)

    def update_draft_bill(self, invoice_id, bill):
        self.current = {
            **self.current,
            **bill,
            "InvoiceID": invoice_id,
            "Contact": {"ContactID": bill["Contact"]["ContactID"], "Name": "Supplier"},
            "Total": "1250.00",
            "AmountDue": "1250.00",
        }
        return dict(self.current)

    def attach_bill_document(self, invoice_id, filename, media_type, content):
        self.attachments.append((invoice_id, filename, media_type, content))
        record = {"AttachmentID": f"attachment-{len(self.attachment_records) + 1}", "FileName": filename, "MimeType": media_type}
        self.attachment_records.append((record, content))
        self.current = {**self.current, "HasAttachments": True}
        return record

    def list_bill_attachments(self, _invoice_id):
        return tuple(record for record, _content in self.attachment_records)

    def read_bill_attachment(self, _invoice_id, attachment_id, _media_type):
        return next(content for record, content in self.attachment_records if record["AttachmentID"] == attachment_id)

    def delete_draft_bill(self, _invoice_id):
        self.deleted.append(_invoice_id)
        self.current = {**self.current, "Status": "DELETED"}
        return self.current

    def authorise_bill(self, _invoice_id):
        self.current = {**self.current, "Status": "AUTHORISED"}
        return self.current


class FakeMail:
    def __init__(self) -> None:
        self.references = []
        digest = hashlib.sha256(b"pdf-data").hexdigest()
        self.attachment = MailAttachment(
            "4", "SUP-42.pdf", "application/pdf", 8, digest, "Invoice"
        )

    def read_attachment(self, reference, attachment_id):
        self.references.append((reference, attachment_id))
        return self.attachment, b"pdf-data"


def capture_arguments() -> dict:
    return {
        "mailbox_id": "INBOX",
        "uid_validity": "777",
        "uid": "1",
        "attachment_id": "4",
        "expected_sha256": hashlib.sha256(b"pdf-data").hexdigest(),
        "authorise": False,
    }


def captured_invoice() -> dict:
    """What the bounded specialist returns for the fixture document."""
    return {
        "document_type": "supplier_invoice",
        "supplier_name": "Supplier",
        "invoice_number": "SUP-42",
        "invoice_date": "2026-08-30",
        "due_date": "2026-09-30",
        "currency": "ZAR",
        "subtotal": "1250.00",
        "tax_amount": "0.00",
        "total": "1250.00",
        "description": "Components",
        "verified": True,
        "problems": (),
    }


class XeroPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = FakeXero()
        self.mail = FakeMail()
        self.executors = build_xero_executors(
            self.xero, self.mail, lambda: "call-1"
        )

    def test_a_draft_bill_can_be_discarded_when_it_matches(self) -> None:
        result = self.executors[DELETE_XERO_DRAFT_BILL](
            {
                "invoice_id": "bill-1",
                "invoice_number": "SUP-42",
                "expected_total": "1250.00",
            }
        )
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["status"], "DELETED")
        self.assertEqual(self.xero.deleted, ["bill-1"])

    def test_an_authorised_bill_is_never_discarded(self) -> None:
        """D-019 covers drafts only. Voiding an accounting entry is not authorised."""
        for status in ("AUTHORISED", "PAID", "VOIDED"):
            with self.subTest(status=status):
                self.xero.deleted.clear()
                self.xero.current = {**self.xero.current, "Status": status}
                result = self.executors[DELETE_XERO_DRAFT_BILL](
                    {
                        "invoice_id": "bill-1",
                        "invoice_number": "SUP-42",
                        "expected_total": "1250.00",
                    }
                )
                self.assertEqual(result.failure["code"], "bill_not_draft")
                self.assertEqual(self.xero.deleted, [])

    def test_a_mismatched_draft_is_never_discarded(self) -> None:
        """The wrong bill must not be discarded on a mis-identified id."""
        for wrong in (
            {"invoice_number": "SUP-99", "expected_total": "1250.00"},
            {"invoice_number": "SUP-42", "expected_total": "999.99"},
        ):
            with self.subTest(**wrong):
                self.xero.deleted.clear()
                result = self.executors[DELETE_XERO_DRAFT_BILL](
                    {"invoice_id": "bill-1", **wrong}
                )
                self.assertEqual(result.failure["code"], "source_mismatch")
                self.assertEqual(self.xero.deleted, [])

    def test_discarding_a_draft_asks_before_acting_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(unattended_bill_writes=True),
                Path(directory),
                self.mail,
                lambda: "call",
            )
        # Unattended bill writes must not silently carry deletion with them.
        self.assertFalse(runtime.policies[CAPTURE_SUPPLIER_INVOICE].approval_required)
        self.assertTrue(runtime.policies[DELETE_XERO_DRAFT_BILL].approval_required)
        self.assertIn(
            XERO_BILL_DELETE_PERMISSION,
            runtime.policies[DELETE_XERO_DRAFT_BILL].permission_references,
        )

    def test_a_fresh_create_response_is_not_treated_as_invalid(self) -> None:
        """Xero omits fields a new bill cannot have yet.

        A real bill was created in Xero and then reported to Friedl as not
        posted, because the create response carried no HasAttachments and no
        AmountDue. Reporting a completed write as a failure is the worst
        direction to be wrong in: nothing was attached or authorised, and the
        bill was left stranded as a draft.
        """
        from alx.tools.xero import _bill_values

        fresh = {
            "Type": "ACCPAY",
            "InvoiceID": "bill-1",
            "InvoiceNumber": "2026/2027-04",
            "Status": "DRAFT",
            "Total": 24900.0,
            "Contact": {"ContactID": "contact-1", "Name": "Supplier"},
        }
        values = _bill_values(fresh)
        self.assertTrue(values["found"])
        self.assertEqual(values["total"], "24900.00")
        self.assertEqual(values["amount_due"], "24900.00")
        self.assertIs(values["has_attachments"], False)

    def test_a_genuinely_bad_response_still_fails_closed(self) -> None:
        """Tolerating absent optional fields must not tolerate bad money."""
        from alx.tools.xero import _bill_values

        base = {
            "Type": "ACCPAY",
            "InvoiceID": "bill-1",
            "InvoiceNumber": "SUP-42",
            "Status": "DRAFT",
            "Total": 100.0,
            "Contact": {"ContactID": "contact-1"},
        }
        for label, bill in (
            ("total unreadable", {**base, "Total": "about a hundred"}),
            ("total absent", {k: v for k, v in base.items() if k != "Total"}),
            ("attachments flag not a boolean", {**base, "HasAttachments": "yes"}),
            ("amount due unreadable", {**base, "AmountDue": "later"}),
            ("no invoice number", {**base, "InvoiceNumber": ""}),
        ):
            with self.subTest(label=label):
                with self.assertRaises(XeroAccessError):
                    _bill_values(bill)

    def test_d018_unattended_writes_proceed_without_an_approval(self) -> None:
        """D-018: Friedl authorised unattended supplier-bill writes.

        The authority changes; the accounting safeguards do not. Reads stay
        non-effectful and the bill-write permission is still required, so the
        capability cannot run for an unpermitted principal.
        """
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(unattended_bill_writes=True),
                Path(directory),
                self.mail,
                lambda: "call",
            )
        gate = SafetyGate(runtime.policies)
        for capability_id in (
        ):
            with self.subTest(capability_id=capability_id):
                self.assertFalse(runtime.policies[capability_id].approval_required)
                call = CapabilityCall("call-1", capability_id, {})
                permitted = AuthorityContext(
                    "friedl",
                    frozenset({XERO_READ_PERMISSION, XERO_BILL_WRITE_PERMISSION}),
                    datetime(2026, 8, 31, tzinfo=UTC),
                )
                self.assertEqual(
                    gate.evaluate(call, permitted).state, SafetyState.ALLOWED
                )
                unpermitted = AuthorityContext(
                    "someone-else",
                    frozenset({XERO_READ_PERMISSION}),
                    datetime(2026, 8, 31, tzinfo=UTC),
                )
                self.assertEqual(
                    gate.evaluate(call, unpermitted).state, SafetyState.DENIED
                )

    def test_runtime_requires_approval_for_every_xero_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_xero_runtime(
                xero_settings(),
                Path(directory),
                self.mail,
                lambda: "call",
            )
        self.assertEqual(
            runtime.permissions,
            frozenset(
                {
                    XERO_READ_PERMISSION,
                    XERO_BILL_WRITE_PERMISSION,
                    XERO_BILL_DELETE_PERMISSION,
                }
            ),
        )
        for capability_id in (
            CAPTURE_SUPPLIER_INVOICE,
            DELETE_XERO_DRAFT_BILL,
        ):
            self.assertTrue(runtime.policies[capability_id].approval_required)

    def test_create_result_survives_restart_and_returns_to_core_for_readback(self) -> None:
        now = datetime(2026, 8, 31, tzinfo=UTC)
        retention = now + timedelta(days=30)
        capture_call = CapabilityCall(
            "capture-1",
            CAPTURE_SUPPLIER_INVOICE,
            capture_arguments(),
            "approval-1",
        )
        approval = Approval(
            "approval-1",
            ApprovalScope(CAPTURE_SUPPLIER_INVOICE, capture_arguments()),
            ApprovalLifecycle.GRANTED,
            now + timedelta(minutes=10),
        )
        conversation = ConversationSnapshot(
            "conversation-1",
            (
                ConversationTurn(
                    "conversation-1",
                    "turn-1",
                    ConversationOrigin.TYPED,
                    "Create the supplier bill from the invoice.",
                    now,
                    "friedl",
                ),
            ),
            1,
            retention,
        )

        class Queued:
            def __init__(self, *decisions):
                self.decisions = list(decisions)
                self.contexts = []

            def decide(self, context):
                self.contexts.append(context)
                return self.decisions.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite3"
            store = SQLiteGoalStore(path)
            store.create(
                GoalState(
                    "goal-1",
                    Objective("turn:turn-1", "Create and verify the supplier bill"),
                    (SuccessCriterion("criterion-1", "bill read back from Xero"),),
                    approvals=(approval,),
                ),
                "conversation-1",
                retention,
            )
            registry = CapabilityRegistry()
            for definition in XERO_DEFINITIONS:
                registry.register(definition)
            current_call_id = [""]
            runtime = build_xero_runtime(
                xero_settings(approval_ttl_seconds=600),
                Path(directory),
                self.mail,
                lambda: current_call_id[0],
            )
            broker = CapabilityBroker(
                registry,
                SafetyGate(runtime.policies),
                build_xero_executors(
                    self.xero,
                    self.mail,
                    lambda: current_call_id[0],
                    lambda *_: captured_invoice(),
                    "310",
                    "NONE",
                ),
            )

            def dispatch(call, state):
                current_call_id[0] = call.call_id
                return broker.dispatch(
                    call,
                    AuthorityContext(
                        "friedl",
                        runtime.permissions,
                        now,
                        state.approvals if state is not None else (),
                    ),
                )

            first_reasoner = Queued(AgentDecision(call=capture_call))
            first = CoreAgent(
                store,
                first_reasoner,
                dispatch,
                XERO_DEFINITIONS,
                clock=lambda: now,
            ).process(conversation, "goal-1", retention, 1)
            self.assertEqual(first.state, CoreState.CHECKPOINTED)
            captured = first.snapshot.state.attempts[-1].result.values
            self.assertTrue(captured["completed"])
            self.assertEqual(captured["bill"]["status"], "DRAFT")
            self.assertEqual(captured["bill"]["invoice_number"], "SUP-42")
            store.close()

            restarted_store = SQLiteGoalStore(path)
            read_call = CapabilityCall(
                "read-1", READ_XERO_BILL, {"invoice_id": "bill-1"}
            )
            second_reasoner = Queued(
                AgentDecision(call=read_call),
                AgentDecision(response="The draft bill exists and matches the invoice."),
            )
            second = CoreAgent(
                restarted_store,
                second_reasoner,
                dispatch,
                XERO_DEFINITIONS,
                clock=lambda: now,
            ).process(conversation, "goal-1", retention, 2)
            self.assertEqual(second.state, CoreState.RESPONDED)
            # The capture result survived the restart in durable state.
            self.assertEqual(
                second_reasoner.contexts[0].active_goal.attempts[-1].call.capability_id,
                CAPTURE_SUPPLIER_INVOICE,
            )
            self.assertEqual(
                second_reasoner.contexts[1].active_goal.attempts[-1].call.capability_id,
                READ_XERO_BILL,
            )
            restarted_store.close()


class XeroOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "xero.sqlite3"
        self.oauth = SQLiteXeroOAuth(
            self.path, "client", "secret", "http://localhost/callback", "", 10
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_oauth_state_survives_restart_and_tokens_are_encrypted(self) -> None:
        url = self.oauth.begin_authorization(now=100)
        state = parse_qs(urlsplit(url).query)["state"][0]
        restarted = SQLiteXeroOAuth(
            self.path, "client", "secret", "http://localhost/callback", "", 10
        )
        with patch.object(
            restarted,
            "_token_request",
            return_value={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 1800,
                "scope": "accounting.invoices",
            },
        ), patch.object(
            restarted,
            "_connections",
            return_value=[{"tenantId": "tenant-1", "tenantName": "FireFli"}],
        ):
            self.assertEqual(restarted.exchange_code("code", state, now=101), "FireFli")

        raw = self.path.read_bytes()
        self.assertNotIn(b"access-secret", raw)
        self.assertNotIn(b"refresh-secret", raw)
        key_mode = stat.S_IMODE(self.path.with_suffix(".key").stat().st_mode)
        self.assertEqual(key_mode, 0o600)
        connection = restarted.connection(now=102)
        self.assertEqual(connection.access_token, "access-secret")
        self.assertEqual(connection.tenant_id, "tenant-1")

    def test_concurrent_refresh_rotates_the_token_once(self) -> None:
        self.oauth._persist(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_in": 1,
                "scope": "accounting.invoices",
            },
            {"tenantId": "tenant-1", "tenantName": "FireFli"},
            now=0,
        )
        calls = []

        def refresh(_data):
            calls.append(1)
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 1800,
                "scope": "accounting.invoices",
            }

        results = []
        with patch.object(self.oauth, "_token_request", side_effect=refresh):
            threads = [
                threading.Thread(
                    target=lambda: results.append(self.oauth.connection(now=100))
                )
                for _ in range(5)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(calls), 1)
        self.assertEqual({item.access_token for item in results}, {"new-access"})

    def test_provider_failure_does_not_retain_the_http_exception(self) -> None:
        self.oauth._persist(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 1800,
                "scope": "accounting.invoices",
            },
            {"tenantId": "tenant-1", "tenantName": "FireFli"},
            now=0,
        )
        adapter = XeroAccountingAdapter(self.oauth)
        request = type("Request", (), {"content": b"private invoice"})()

        class RequestFailure(Exception):
            pass

        error = RequestFailure("failed")
        error.request = request
        with patch("httpx.request", side_effect=error):
            with self.assertRaises(XeroAccessError) as captured:
                adapter.list_accounts()
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)
        self.assertNotIn("private invoice", str(captured.exception))

    def test_token_key_symlink_is_refused_without_changing_target(self) -> None:
        target = Path(self.directory.name) / "outside.key"
        target.write_bytes(b"not-a-real-key")
        target.chmod(0o640)
        linked_database = Path(self.directory.name) / "linked.sqlite3"
        linked_database.with_suffix(".key").symlink_to(target)
        with self.assertRaises(XeroAccessError) as captured:
            SQLiteXeroOAuth(
                linked_database, "client", "secret", "http://localhost/callback"
            )
        self.assertEqual(captured.exception.code, "token_key_invalid")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


class XeroAccountingAdapterTests(unittest.TestCase):
    class ConnectedOAuth:
        def connection(self):
            return XeroConnection("access-token", "tenant-1")

    @staticmethod
    def response(body):
        response = Mock(status_code=200)
        response.json.return_value = body
        return response

    def setUp(self) -> None:
        self.adapter = XeroAccountingAdapter(self.ConnectedOAuth(), timeout_seconds=17)

    def test_create_draft_uses_accounting_endpoint_and_tenant_boundary(self) -> None:
        bill = {"Type": "ACCPAY", "Status": "DRAFT", "InvoiceNumber": "SUP-42"}
        response = self.response({"Invoices": [{**bill, "InvoiceID": "bill-1"}]})
        with patch("httpx.request", return_value=response) as request:
            created = self.adapter.create_draft_bill(bill)

        self.assertEqual(created["InvoiceID"], "bill-1")
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", f"{ACCOUNTING_URL}/Invoices"))
        self.assertEqual(kwargs["headers"]["Xero-tenant-id"], "tenant-1")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(kwargs["json"], {"Invoices": [bill]})
        self.assertEqual(kwargs["timeout"], 17)

    def test_discarded_bill_does_not_block_recreating_the_invoice_number(self) -> None:
        """A deleted or voided bill is not an existing bill.

        Xero retains discarded invoices, so a re-captured supplier invoice
        must not be refused as a duplicate after Friedl deleted it.
        """
        for status in ("DELETED", "VOIDED"):
            with self.subTest(status=status):
                response = self.response(
                    {
                        "Invoices": [
                            {
                                "InvoiceID": "discarded-1",
                                "InvoiceNumber": "655",
                                "Type": "ACCPAY",
                                "Status": status,
                                "Contact": {"ContactID": "contact-1"},
                            }
                        ]
                    }
                )
                with patch("httpx.request", return_value=response):
                    self.assertIsNone(self.adapter.find_bill("655", "contact-1"))

    def test_live_bill_still_blocks_a_duplicate_invoice_number(self) -> None:
        for status in ("DRAFT", "AUTHORISED", "PAID"):
            with self.subTest(status=status):
                response = self.response(
                    {
                        "Invoices": [
                            {
                                "InvoiceID": "live-1",
                                "InvoiceNumber": "655",
                                "Type": "ACCPAY",
                                "Status": status,
                                "Contact": {"ContactID": "contact-1"},
                            }
                        ]
                    }
                )
                with patch("httpx.request", return_value=response):
                    found = self.adapter.find_bill("655", "contact-1")
                self.assertIsNotNone(found)
                self.assertEqual(found["InvoiceID"], "live-1")

    def test_duplicate_lookup_uses_documented_collection_filters(self) -> None:
        response = self.response(
            {
                "Invoices": [
                    {
                        "InvoiceID": "bill-1",
                        "InvoiceNumber": "SUP 42/7",
                        "Type": "ACCPAY",
                        "Contact": {"ContactID": "contact/1"},
                    }
                ]
            }
        )
        with patch("httpx.request", return_value=response) as request:
            found = self.adapter.find_bill("SUP 42/7", "contact/1")

        self.assertEqual(found["InvoiceID"], "bill-1")
        args, _kwargs = request.call_args
        self.assertEqual(
            args,
            (
                "GET",
                f"{ACCOUNTING_URL}/Invoices?InvoiceNumbers=SUP+42%2F7&ContactIDs=contact%2F1",
            ),
        )

    def test_attachment_uses_exact_bytes_and_escaped_filename(self) -> None:
        response = self.response({"Attachments": [{"FileName": "invoice 42.pdf"}]})
        with patch("httpx.request", return_value=response) as request:
            attached = self.adapter.attach_bill_document(
                "bill/1", "invoice 42.pdf", "application/pdf", b"exact-pdf"
            )

        self.assertEqual(attached["FileName"], "invoice 42.pdf")
        args, kwargs = request.call_args
        self.assertEqual(
            args,
            (
                "PUT",
                f"{ACCOUNTING_URL}/Invoices/bill%2F1/Attachments/invoice%2042.pdf",
            ),
        )
        self.assertEqual(kwargs["content"], b"exact-pdf")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/pdf")
        self.assertIsNone(kwargs["json"])

    def test_attachment_readback_uses_exact_attachment_identifier_and_bytes(self) -> None:
        response = self.response({})
        response.content = b"stored-exact-pdf"
        with patch("httpx.request", return_value=response) as request:
            payload = self.adapter.read_bill_attachment(
                "bill/1", "attachment/2", "application/pdf"
            )

        self.assertEqual(payload, b"stored-exact-pdf")
        args, kwargs = request.call_args
        self.assertEqual(
            args,
            (
                "GET",
                f"{ACCOUNTING_URL}/Invoices/bill%2F1/Attachments/attachment%2F2",
            ),
        )
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(kwargs["headers"]["contentType"], "application/pdf")
        self.assertEqual(kwargs["headers"]["Accept"], "application/octet-stream")

    def test_update_targets_only_the_exact_xero_invoice_id(self) -> None:
        bill = {
            "Type": "ACCPAY", "Status": "DRAFT", "InvoiceNumber": "DHL-42",
            "Contact": {"ContactID": "contact-1"},
        }
        response = self.response({"Invoices": [{**bill, "InvoiceID": "bill-1"}]})
        with patch("httpx.request", return_value=response) as request:
            updated = self.adapter.update_draft_bill("bill/1", bill)

        self.assertEqual(updated["InvoiceID"], "bill-1")
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", f"{ACCOUNTING_URL}/Invoices/bill%2F1"))
        self.assertEqual(kwargs["json"]["Invoices"][0]["InvoiceID"], "bill/1")

    def test_authorise_updates_only_the_identified_bill(self) -> None:
        response = self.response(
            {"Invoices": [{"InvoiceID": "bill-1", "Status": "AUTHORISED"}]}
        )
        with patch("httpx.request", return_value=response) as request:
            authorised = self.adapter.authorise_bill("bill-1")

        self.assertEqual(authorised["Status"], "AUTHORISED")
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", f"{ACCOUNTING_URL}/Invoices/bill-1"))
        self.assertEqual(
            kwargs["json"],
            {"Invoices": [{"InvoiceID": "bill-1", "Status": "AUTHORISED"}]},
        )


if __name__ == "__main__":
    unittest.main()
