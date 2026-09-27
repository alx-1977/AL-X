"""D-034: AL/X renames one existing Xero contact and carries on herself.

An Izwi invoice could not be captured because the Xero contact was named
IzwiTech, and AL/X could only ask Friedl to rename it by hand. She now decides
the rename and a deterministic capability performs exactly that change: the
contact must exist under the ContactID given, nothing but its Name is sent,
no contact is created or merged, and the result is read back.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.xero import (  # noqa: E402
    XERO_BILL_WRITE_PERMISSION,
    XERO_CONTACT_RENAME_PERMISSION,
    XERO_READ_PERMISSION,
    build_xero_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision,
    CapabilityCall,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalState,
    Objective,
    SideEffect,
    SuccessCriterion,
    XeroAccessError,
)
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.providers.xero import (  # noqa: E402
    ACCOUNTING_URL,
    XERO_SCOPES,
    XeroAccountingAdapter,
    XeroConnection,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
    SEARCH_XERO_CONTACTS,
    UPDATE_XERO_CONTACT,
    XERO_DEFINITIONS,
    build_xero_executors,
)
from support import xero_settings  # noqa: E402
from test_invoice_capture import FakeMail, FakeXero, arguments, extracted  # noqa: E402

IZWI_ID = "7c1f0e3a-0000-4000-8000-000000000001"
OTHER_ID = "7c1f0e3a-0000-4000-8000-000000000002"
LEGAL_NAME = "Izwi Technology Group (Pty) Ltd"


def izwi() -> dict:
    return {
        "ContactID": IZWI_ID,
        "Name": "IzwiTech",
        "ContactStatus": "ACTIVE",
        "EmailAddress": "accounts@izwi.example",
        "TaxNumber": "4123456789",
        "BankAccountDetails": "000111222",
        "Addresses": [{"AddressType": "POBOX", "City": "Johannesburg"}],
        "IsSupplier": True,
    }


class ContactXero(FakeXero):
    """Xero's contact semantics: an update changes only the fields it sends."""

    def __init__(self, *contacts: dict) -> None:
        super().__init__()
        self.records = {item["ContactID"]: dict(item) for item in contacts}
        self.renames: list[tuple[str, str]] = []
        self.fail_rename: str = ""

    @property
    def contacts(self):
        return tuple(dict(item) for item in self.records.values())

    @contacts.setter
    def contacts(self, _value) -> None:
        pass

    def search_contacts(self, term, include_archived=False):
        wanted = term.casefold()
        return tuple(
            dict(item)
            for item in self.records.values()
            # Xero's SearchTerm is a contains-match, so the invoice's legal
            # name does not find the contact while it is still IzwiTech.
            if wanted in item["Name"].casefold()
            # Xero leaves archived contacts out unless asked for them.
            and (include_archived or item["ContactStatus"] != "ARCHIVED")
        )

    def read_contact(self, contact_id):
        record = self.records.get(contact_id)
        return dict(record) if record is not None else None

    def rename_contact(self, contact_id, name):
        if self.fail_rename:
            raise XeroAccessError(self.fail_rename)
        self.renames.append((contact_id, name))
        self.records[contact_id] = {**self.records[contact_id], "Name": name}
        return dict(self.records[contact_id])


class UpdateContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = ContactXero(izwi())
        self.update = build_xero_executors(self.xero, FakeMail(), lambda: "call-1")[
            UPDATE_XERO_CONTACT
        ]

    def test_a_contact_is_renamed_and_read_back(self) -> None:
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(
            result.values,
            {
                "contact_id": IZWI_ID,
                "name": LEGAL_NAME,
                "previous_name": "IzwiTech",
                "status": "ACTIVE",
                "changed": True,
            },
        )
        self.assertEqual(self.xero.renames, [(IZWI_ID, LEGAL_NAME)])

    def test_an_unknown_contact_is_refused_without_a_write(self) -> None:
        result = self.update({"contact_id": OTHER_ID, "name": LEGAL_NAME})
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "contact_not_found")
        self.assertEqual(self.xero.renames, [])

    def test_a_provider_rejection_is_reported_not_absorbed(self) -> None:
        for code in ("permission_denied", "request_rejected", "rate_limited"):
            with self.subTest(code=code):
                self.xero.fail_rename = code
                result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], code)
                self.assertEqual(self.xero.records[IZWI_ID]["Name"], "IzwiTech")

    def test_every_field_not_supplied_is_preserved(self) -> None:
        before = izwi()
        self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        after = self.xero.records[IZWI_ID]
        self.assertEqual(after["Name"], LEGAL_NAME)
        self.assertEqual(
            {key: value for key, value in after.items() if key != "Name"},
            {key: value for key, value in before.items() if key != "Name"},
        )

    def test_no_contact_is_created_or_merged(self) -> None:
        self.xero = ContactXero(izwi(), {**izwi(), "ContactID": OTHER_ID, "Name": "Acme"})
        update = build_xero_executors(self.xero, FakeMail(), lambda: "c")[
            UPDATE_XERO_CONTACT
        ]
        update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(set(self.xero.records), {IZWI_ID, OTHER_ID})
        self.assertEqual(self.xero.records[OTHER_ID]["Name"], "Acme")

    def test_a_name_another_contact_holds_is_refused(self) -> None:
        """Two contacts sharing a name is ambiguous identity, never a merge."""
        self.xero.records[OTHER_ID] = {
            **izwi(),
            "ContactID": OTHER_ID,
            "Name": LEGAL_NAME.upper(),
        }
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.failure["code"], "contact_name_conflict")
        self.assertEqual(self.xero.renames, [])

    def test_an_archived_namesake_is_refused(self) -> None:
        """Xero would allow it; D-034 treats it as ambiguous identity."""
        self.xero.records[OTHER_ID] = {
            **izwi(),
            "ContactID": OTHER_ID,
            "Name": LEGAL_NAME,
            "ContactStatus": "ARCHIVED",
        }
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "contact_name_conflict")
        self.assertEqual(self.xero.renames, [])
        self.assertEqual(self.xero.records[IZWI_ID]["Name"], "IzwiTech")

    def test_an_archived_contact_with_another_name_does_not_block(self) -> None:
        self.xero.records[OTHER_ID] = {
            **izwi(),
            "ContactID": OTHER_ID,
            "Name": "Izwi Technology Group (Pty) Ltd - old",
            "ContactStatus": "ARCHIVED",
        }
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.xero.renames, [(IZWI_ID, LEGAL_NAME)])

    def test_a_name_already_in_place_writes_nothing(self) -> None:
        """A retry after an unseen success must not write again."""
        self.xero.records[IZWI_ID]["Name"] = LEGAL_NAME
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(result.values["changed"])
        self.assertEqual(self.xero.renames, [])

    def test_a_read_back_that_disagrees_is_not_success(self) -> None:
        def ignored(contact_id, _name):
            self.xero.renames.append((contact_id, _name))
            return dict(self.xero.records[contact_id])

        self.xero.rename_contact = ignored
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.failure["code"], "read_back_mismatch")

    def test_a_different_contact_in_the_response_is_not_success(self) -> None:
        self.xero.rename_contact = lambda _id, name: {"ContactID": OTHER_ID, "Name": name}
        result = self.update({"contact_id": IZWI_ID, "name": LEGAL_NAME})
        self.assertEqual(result.failure["code"], "read_back_mismatch")

    def test_unusable_arguments_are_refused(self) -> None:
        for bad in (
            {"contact_id": IZWI_ID, "name": "   "},
            {"contact_id": "", "name": LEGAL_NAME},
            {"contact_id": IZWI_ID, "name": "x" * 256},
            {"contact_id": IZWI_ID},
        ):
            with self.subTest(bad=bad):
                result = self.update(bad)
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.xero.renames, [])


class AdapterTests(unittest.TestCase):
    class ConnectedOAuth:
        def connection(self):
            return XeroConnection("access-token", "tenant-1")

    @staticmethod
    def response(body, status_code=200):
        response = Mock(status_code=status_code)
        response.json.return_value = body
        return response

    def setUp(self) -> None:
        self.adapter = XeroAccountingAdapter(self.ConnectedOAuth())

    def test_rename_updates_by_contact_id_and_sends_only_the_name(self) -> None:
        response = self.response({"Contacts": [{"ContactID": IZWI_ID, "Name": LEGAL_NAME}]})
        with patch("httpx.request", return_value=response) as request:
            self.adapter.rename_contact(IZWI_ID, LEGAL_NAME)
        args, kwargs = request.call_args
        # A POST to /Contacts without the identifier would create a contact.
        self.assertEqual(args, ("POST", f"{ACCOUNTING_URL}/Contacts/{IZWI_ID}"))
        self.assertEqual(
            kwargs["json"], {"Contacts": [{"ContactID": IZWI_ID, "Name": LEGAL_NAME}]}
        )

    def test_only_the_conflict_search_asks_for_archived_contacts(self) -> None:
        response = self.response({"Contacts": []})
        with patch("httpx.request", return_value=response) as request:
            self.adapter.search_contacts(LEGAL_NAME)
            ordinary = request.call_args.args[1]
            self.adapter.search_contacts(LEGAL_NAME, include_archived=True)
            conflict = request.call_args.args[1]
        self.assertNotIn("includeArchived", ordinary)
        self.assertEqual(conflict, f"{ordinary}&includeArchived=true")

    def test_an_unknown_contact_reads_as_absent(self) -> None:
        with patch("httpx.request", return_value=self.response({}, 404)):
            self.assertIsNone(self.adapter.read_contact(IZWI_ID))

    def test_a_response_for_another_contact_reads_as_absent(self) -> None:
        response = self.response({"Contacts": [{"ContactID": OTHER_ID, "Name": "Acme"}]})
        with patch("httpx.request", return_value=response):
            self.assertIsNone(self.adapter.read_contact(IZWI_ID))

    def test_provider_refusals_surface_as_codes(self) -> None:
        for status, code in ((403, "permission_denied"), (400, "request_rejected")):
            with self.subTest(status=status):
                with patch("httpx.request", return_value=self.response({}, status)):
                    with self.assertRaises(XeroAccessError) as raised:
                        self.adapter.rename_contact(IZWI_ID, LEGAL_NAME)
                self.assertEqual(raised.exception.code, code)

    def test_the_contact_write_scope_is_requested(self) -> None:
        self.assertIn("accounting.contacts", XERO_SCOPES)
        # D-034 changes contacts only.
        self.assertNotIn("accounting.payments", XERO_SCOPES)
        self.assertNotIn("accounting.banktransactions", XERO_SCOPES)


class CatalogueTests(unittest.TestCase):
    def runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            return build_xero_runtime(
                xero_settings(), Path(directory), FakeMail(), lambda: "call"
            )

    def test_the_capability_is_in_the_authoritative_catalogue(self) -> None:
        definition = next(
            item for item in XERO_DEFINITIONS if item.capability_id == UPDATE_XERO_CONTACT
        )
        self.assertIs(definition.side_effect, SideEffect.EFFECTFUL)
        self.assertEqual(set(definition.input_schema.properties), {"contact_id", "name"})
        self.assertEqual(definition.input_schema.required, ("contact_id", "name"))
        self.assertFalse(definition.input_schema.extra_properties)
        runtime = self.runtime()
        self.assertIn(definition, runtime.definitions)
        self.assertIn(UPDATE_XERO_CONTACT, runtime.executors)

    def test_d034_standing_authority_needs_its_own_permission(self) -> None:
        runtime = self.runtime()
        policy = runtime.policies[UPDATE_XERO_CONTACT]
        self.assertFalse(policy.approval_required)
        self.assertEqual(policy.permission_references, {XERO_CONTACT_RENAME_PERMISSION})
        self.assertIn(XERO_CONTACT_RENAME_PERMISSION, runtime.permissions)
        call = CapabilityCall("c", UPDATE_XERO_CONTACT, {})
        now = datetime(2026, 9, 27, tzinfo=UTC)
        gate = SafetyGate(runtime.policies)
        self.assertEqual(
            gate.evaluate(call, AuthorityContext("friedl", runtime.permissions, now)).state,
            SafetyState.ALLOWED,
        )
        # Bill-write authority does not carry contact writes with it.
        bill_only = frozenset({XERO_READ_PERMISSION, XERO_BILL_WRITE_PERMISSION})
        self.assertEqual(
            gate.evaluate(call, AuthorityContext("friedl", bill_only, now)).state,
            SafetyState.DENIED,
        )


class ContinuationTests(unittest.TestCase):
    """The blocked invoice continues after the rename with no manual edit.

    The reasoner is scripted: this proves the capabilities compose through
    the one Core loop, broker and safety gate, not that a model chooses them.
    """

    def test_capture_completes_after_alx_renames_the_contact(self) -> None:
        now = datetime(2026, 9, 27, tzinfo=UTC)
        retention = now + timedelta(days=30)
        xero = ContactXero(izwi())
        xero.history = (
            {"LineAmountTypes": "NoTax", "LineItems": [{"AccountCode": "310", "TaxType": "NONE"}]},
        )
        mail = FakeMail()
        extractor = lambda *_: extracted(supplier_name=LEGAL_NAME)  # noqa: E731
        conversation = ConversationSnapshot(
            "conversation-1",
            (
                ConversationTurn(
                    "conversation-1",
                    "turn-1",
                    ConversationOrigin.TYPED,
                    "Capture the Izwi invoice.",
                    now,
                    "friedl",
                ),
            ),
            1,
            retention,
        )
        capture = CapabilityCall("capture-1", CAPTURE_SUPPLIER_INVOICE, arguments())
        search = CapabilityCall("search-1", SEARCH_XERO_CONTACTS, {"search_term": "Izwi"})
        rename = CapabilityCall(
            "rename-1", UPDATE_XERO_CONTACT, {"contact_id": IZWI_ID, "name": LEGAL_NAME}
        )
        retry = CapabilityCall("capture-2", CAPTURE_SUPPLIER_INVOICE, arguments())

        class Queued:
            def __init__(self, *decisions):
                self.decisions = list(decisions)

            def decide(self, _context):
                return self.decisions.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            store.create(
                GoalState(
                    "goal-1",
                    Objective("turn:turn-1", "Capture the Izwi supplier invoice"),
                    (SuccessCriterion("criterion-1", "bill authorised and read back"),),
                ),
                "conversation-1",
                retention,
            )
            registry = CapabilityRegistry()
            for definition in XERO_DEFINITIONS:
                registry.register(definition)
            current = [""]
            runtime = build_xero_runtime(
                xero_settings(unattended_bill_writes=True),
                Path(directory),
                mail,
                lambda: current[0],
                extractor,
            )
            broker = CapabilityBroker(
                registry,
                SafetyGate(runtime.policies),
                build_xero_executors(xero, mail, lambda: current[0], extractor),
            )

            def dispatch(call, state):
                current[0] = call.call_id
                # No approval record is supplied: D-018 and D-034 are standing.
                return broker.dispatch(
                    call, AuthorityContext("friedl", runtime.permissions, now)
                )

            outcome = CoreAgent(
                store,
                Queued(
                    AgentDecision(call=capture, goal_id="goal-1"),
                    AgentDecision(call=search, goal_id="goal-1"),
                    AgentDecision(call=rename, goal_id="goal-1"),
                    AgentDecision(call=retry, goal_id="goal-1"),
                    AgentDecision(response="Izwi's bill is posted.", goal_id="goal-1"),
                ),
                dispatch,
                XERO_DEFINITIONS,
                clock=lambda: now,
            ).process(conversation, retention, 6)
            store.close()

        self.assertEqual(outcome.state, CoreState.RESPONDED)
        attempts = outcome.snapshot.state.attempts
        first, renamed, second = (
            next(item for item in attempts if item.call.call_id == call_id).result
            for call_id in ("capture-1", "rename-1", "capture-2")
        )
        self.assertFalse(first.values["completed"])
        self.assertEqual(first.values["returned_for"], "supplier_unresolved")
        self.assertTrue(renamed.values["changed"])
        self.assertTrue(second.values["completed"])
        self.assertEqual(second.values["bill"]["contact_id"], IZWI_ID)
        self.assertEqual(second.values["bill"]["status"], "AUTHORISED")
        self.assertEqual(xero.records[IZWI_ID]["Name"], LEGAL_NAME)
        self.assertEqual(xero.created, 1)


if __name__ == "__main__":
    unittest.main()
