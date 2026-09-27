"""D-035: AL/X creates one supplier contact and carries on herself.

A CodeRabbit Inc invoice could not be captured because no Xero contact
existed, and AL/X could only ask Friedl to create one by hand. She now decides
that a new contact is right and a deterministic capability performs exactly
that: a name no contact already holds, sent alone, and read back.
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
    XERO_CONTACT_CREATE_PERMISSION,
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
    XeroAccountingAdapter,
    XeroConnection,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
    CREATE_XERO_CONTACT,
    SEARCH_XERO_CONTACTS,
    UPDATE_XERO_CONTACT,
    XERO_DEFINITIONS,
    build_xero_executors,
)
from support import xero_settings  # noqa: E402
from test_invoice_capture import FakeMail, arguments, extracted  # noqa: E402
from test_xero_contact_rename import ContactXero, izwi  # noqa: E402

SUPPLIER = "CodeRabbit Inc"
NEW_ID = "7c1f0e3a-0000-4000-8000-0000000000c1"


class CreatingXero(ContactXero):
    """Xero's create semantics: a new ContactID carrying only what was sent."""

    def __init__(self, *contacts: dict) -> None:
        super().__init__(*contacts)
        self.creates: list[str] = []
        self.fail_create = ""

    def create_contact(self, name):
        if self.fail_create:
            raise XeroAccessError(self.fail_create)
        self.creates.append(name)
        record = {"ContactID": NEW_ID, "Name": name, "ContactStatus": "ACTIVE"}
        self.records[NEW_ID] = record
        return dict(record)


class CreateContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero = CreatingXero(izwi())
        self.create = build_xero_executors(self.xero, FakeMail(), lambda: "call-1")[
            CREATE_XERO_CONTACT
        ]

    def test_a_contact_is_created_and_read_back(self) -> None:
        result = self.create({"name": SUPPLIER})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(
            result.values,
            {"contact_id": NEW_ID, "name": SUPPLIER, "status": "ACTIVE", "created": True},
        )
        self.assertEqual(self.xero.creates, [SUPPLIER])
        # The new contact carries only its name; nothing else was invented.
        self.assertEqual(
            self.xero.records[NEW_ID],
            {"ContactID": NEW_ID, "Name": SUPPLIER, "ContactStatus": "ACTIVE"},
        )

    def test_an_existing_active_namesake_is_refused(self) -> None:
        for existing in (SUPPLIER, SUPPLIER.upper(), f"  {SUPPLIER.lower()} "):
            with self.subTest(existing=existing):
                self.xero.records["other"] = {
                    **izwi(),
                    "ContactID": "other",
                    "Name": existing,
                }
                result = self.create({"name": SUPPLIER})
                self.assertEqual(result.failure["code"], "contact_name_conflict")
                self.assertEqual(self.xero.creates, [])

    def test_an_archived_namesake_is_refused(self) -> None:
        self.xero.records["other"] = {
            **izwi(),
            "ContactID": "other",
            "Name": SUPPLIER,
            "ContactStatus": "ARCHIVED",
        }
        result = self.create({"name": SUPPLIER})
        self.assertEqual(result.failure["code"], "contact_name_conflict")
        self.assertEqual(self.xero.creates, [])

    def test_a_similar_but_different_name_is_alxs_judgement(self) -> None:
        """Code refuses only a name a contact already holds.

        Whether "CodeRabbit" is the same supplier as "CodeRabbit Inc" is a
        judgement about business identity, so it belongs to AL/X, who sees
        the search results before she asks for creation. The existing
        exact-name conflict logic does not treat it as ambiguous.
        """
        self.xero.records["other"] = {**izwi(), "ContactID": "other", "Name": "CodeRabbit"}
        result = self.create({"name": SUPPLIER})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.xero.creates, [SUPPLIER])

    def test_a_provider_refusal_is_reported_and_nothing_is_retried(self) -> None:
        for code in ("permission_denied", "request_rejected"):
            with self.subTest(code=code):
                self.xero.fail_create = code
                result = self.create({"name": SUPPLIER})
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], code)
                self.assertNotIn(NEW_ID, self.xero.records)

    def test_a_retry_after_success_creates_nothing(self) -> None:
        first = self.create({"name": SUPPLIER})
        second = self.create({"name": SUPPLIER})
        self.assertEqual(first.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(second.failure["code"], "contact_name_conflict")
        self.assertEqual(self.xero.creates, [SUPPLIER])

    def test_unusable_names_are_refused(self) -> None:
        for bad in (
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 256},
            {"name": "CodeRabbit\nInc"},
            {"name": 42},
            {},
        ):
            with self.subTest(bad=bad):
                result = self.create(bad)
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.xero.creates, [])

    def test_a_read_back_that_disagrees_is_not_success(self) -> None:
        original = self.xero.read_contact
        self.xero.read_contact = lambda contact_id: (
            {**original(contact_id), "Name": "Something Else"}
            if contact_id == NEW_ID
            else original(contact_id)
        )
        result = self.create({"name": SUPPLIER})
        self.assertEqual(result.failure["code"], "read_back_mismatch")

    def test_a_response_without_a_contact_id_is_not_success(self) -> None:
        self.xero.create_contact = lambda name: {"Name": name}
        result = self.create({"name": SUPPLIER})
        self.assertEqual(result.failure["code"], "read_back_mismatch")


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

    def test_create_puts_only_the_name(self) -> None:
        response = self.response({"Contacts": [{"ContactID": NEW_ID, "Name": SUPPLIER}]})
        with patch("httpx.request", return_value=response) as request:
            created = self.adapter.create_contact(SUPPLIER)
        self.assertEqual(created["ContactID"], NEW_ID)
        args, kwargs = request.call_args
        # PUT only creates; POST /Contacts could update an existing contact.
        self.assertEqual(args, ("PUT", f"{ACCOUNTING_URL}/Contacts"))
        self.assertEqual(kwargs["json"], {"Contacts": [{"Name": SUPPLIER}]})

    def test_provider_refusals_surface_as_codes(self) -> None:
        for status, code in ((403, "permission_denied"), (400, "request_rejected")):
            with self.subTest(status=status):
                with patch("httpx.request", return_value=self.response({}, status)):
                    with self.assertRaises(XeroAccessError) as raised:
                        self.adapter.create_contact(SUPPLIER)
                self.assertEqual(raised.exception.code, code)


class AuthorityTests(unittest.TestCase):
    now = datetime(2026, 9, 27, tzinfo=UTC)

    def runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            return build_xero_runtime(
                xero_settings(), Path(directory), FakeMail(), lambda: "call"
            )

    def state(self, gate, capability_id, permissions):
        call = CapabilityCall("c", capability_id, {})
        return gate.evaluate(call, AuthorityContext("friedl", permissions, self.now)).state

    def test_the_capability_is_in_the_authoritative_catalogue(self) -> None:
        definition = next(
            item for item in XERO_DEFINITIONS if item.capability_id == CREATE_XERO_CONTACT
        )
        self.assertIs(definition.side_effect, SideEffect.EFFECTFUL)
        self.assertEqual(set(definition.input_schema.properties), {"name"})
        self.assertEqual(definition.input_schema.required, ("name",))
        self.assertFalse(definition.input_schema.extra_properties)
        runtime = self.runtime()
        self.assertIn(definition, runtime.definitions)
        self.assertIn(CREATE_XERO_CONTACT, runtime.executors)

    def test_d035_is_standing_authority_under_its_own_permission(self) -> None:
        runtime = self.runtime()
        policy = runtime.policies[CREATE_XERO_CONTACT]
        self.assertFalse(policy.approval_required)
        self.assertEqual(policy.permission_references, {XERO_CONTACT_CREATE_PERMISSION})
        self.assertIn(XERO_CONTACT_CREATE_PERMISSION, runtime.permissions)
        gate = SafetyGate(runtime.policies)
        self.assertEqual(
            self.state(gate, CREATE_XERO_CONTACT, runtime.permissions),
            SafetyState.ALLOWED,
        )

    def test_d034_rename_authority_does_not_permit_creation(self) -> None:
        gate = SafetyGate(self.runtime().policies)
        rename_only = frozenset({XERO_READ_PERMISSION, XERO_CONTACT_RENAME_PERMISSION})
        self.assertEqual(
            self.state(gate, CREATE_XERO_CONTACT, rename_only), SafetyState.DENIED
        )
        bill_only = frozenset({XERO_READ_PERMISSION, XERO_BILL_WRITE_PERMISSION})
        self.assertEqual(
            self.state(gate, CREATE_XERO_CONTACT, bill_only), SafetyState.DENIED
        )

    def test_d035_creation_authority_permits_only_creation(self) -> None:
        gate = SafetyGate(self.runtime().policies)
        create_only = frozenset({XERO_CONTACT_CREATE_PERMISSION})
        self.assertEqual(
            self.state(gate, CREATE_XERO_CONTACT, create_only), SafetyState.ALLOWED
        )
        for other in (UPDATE_XERO_CONTACT, CAPTURE_SUPPLIER_INVOICE, SEARCH_XERO_CONTACTS):
            with self.subTest(capability_id=other):
                self.assertEqual(self.state(gate, other, create_only), SafetyState.DENIED)


class ContinuationTests(unittest.TestCase):
    """The blocked invoice continues after creation with no manual step.

    The reasoner is scripted: this proves the capabilities compose through
    the one Core loop, broker and safety gate, not that a model chooses them.
    """

    def test_capture_completes_against_the_created_contact(self) -> None:
        now = datetime(2026, 9, 27, tzinfo=UTC)
        retention = now + timedelta(days=30)
        xero = CreatingXero(izwi())
        # A new supplier has no bills of its own, so the D-020 default applies.
        xero.history = ()
        mail = FakeMail()
        extractor = lambda *_: extracted(supplier_name=SUPPLIER)  # noqa: E731
        conversation = ConversationSnapshot(
            "conversation-1",
            (
                ConversationTurn(
                    "conversation-1",
                    "turn-1",
                    ConversationOrigin.TYPED,
                    "Capture the CodeRabbit invoice.",
                    now,
                    "friedl",
                ),
            ),
            1,
            retention,
        )
        capture = CapabilityCall("capture-1", CAPTURE_SUPPLIER_INVOICE, arguments())
        search = CapabilityCall(
            "search-1", SEARCH_XERO_CONTACTS, {"search_term": "CodeRabbit"}
        )
        create = CapabilityCall("create-1", CREATE_XERO_CONTACT, {"name": SUPPLIER})
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
                    Objective("turn:turn-1", "Capture the CodeRabbit supplier invoice"),
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
                build_xero_executors(
                    xero, mail, lambda: current[0], extractor, "310", "NONE"
                ),
            )

            def dispatch(call, state):
                current[0] = call.call_id
                # No approval record is supplied: D-018 and D-035 are standing.
                return broker.dispatch(
                    call, AuthorityContext("friedl", runtime.permissions, now)
                )

            outcome = CoreAgent(
                store,
                Queued(
                    AgentDecision(call=capture, goal_id="goal-1"),
                    AgentDecision(call=search, goal_id="goal-1"),
                    AgentDecision(call=create, goal_id="goal-1"),
                    AgentDecision(call=retry, goal_id="goal-1"),
                    AgentDecision(response="CodeRabbit's bill is posted.", goal_id="goal-1"),
                ),
                dispatch,
                XERO_DEFINITIONS,
                clock=lambda: now,
            ).process(conversation, retention, 6)
            store.close()

        self.assertEqual(outcome.state, CoreState.RESPONDED)
        results = {
            item.call.call_id: item.result for item in outcome.snapshot.state.attempts
        }
        self.assertEqual(results["capture-1"].values["returned_for"], "supplier_unresolved")
        self.assertEqual(results["search-1"].values["contacts"], ())
        self.assertEqual(results["create-1"].values["contact_id"], NEW_ID)
        second = results["capture-2"].values
        self.assertTrue(second["completed"])
        self.assertEqual(second["bill"]["contact_id"], NEW_ID)
        self.assertEqual(second["bill"]["status"], "AUTHORISED")
        self.assertEqual(xero.creates, [SUPPLIER])
        self.assertEqual(xero.created, 1)


if __name__ == "__main__":
    unittest.main()
