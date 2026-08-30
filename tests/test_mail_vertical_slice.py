from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision,
    ApprovalLifecycle,
    ApprovalProposal,
    ApprovalScope,
    BackgroundEvent,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    GoalState,
    MailContent,
    MailReference,
    Objective,
    SuccessCriterion,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.providers import ICloudMailAdapter, SQLiteMailObservationState  # noqa: E402
from alx.safety import (  # noqa: E402
    AuthorityContext,
    AuthorityPolicy,
    SafetyGate,
)
from alx.tools import (  # noqa: E402
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    READ_MAIL_MESSAGE,
    build_mail_executors,
)


NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


def message(subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{subject}@example.test>\r\n"
        f"Subject: {subject}\r\n"
        "From: Supplier <supplier@example.test>\r\n"
        "Date: Sat, 29 Aug 2026 10:00:00 +0200\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}"
    ).encode()


class FakeImap:
    def __init__(self) -> None:
        self.items = {1: message("Old", "Old body")}
        self.commands = []

    def login(self, address, secret):
        self.commands.append(("LOGIN", address, secret))
        return "OK", []

    def logout(self):
        return "BYE", []

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [str(len(self.items)).encode()]

    def response(self, name):
        return name, [b"777"]

    def uid(self, operation, *values):
        self.commands.append(("UID", operation, *values))
        if operation == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.items))]
        if operation == "fetch":
            uid = int(values[0])
            return "OK", [(b"metadata", self.items[uid]), b")"]
        if operation == "MOVE":
            return "OK", [b""]
        raise AssertionError(operation)

    def list(self):
        return "OK", [b'(\\Trash) "/" "Deleted Messages"']


class FakeAccount:
    def __init__(self) -> None:
        self.acknowledged = []
        self.trashed = []

    def read(self, reference):
        return MailContent(reference, "Quote", "Supplier", "today", "private body")

    def acknowledge(self, reference):
        self.acknowledged.append(reference)

    def move_to_trash(self, reference):
        self.trashed.append(reference)
        return "Deleted Messages"


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class MailProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.state = SQLiteMailObservationState(
            Path(self.directory.name) / "observations.sqlite3"
        )
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test",
            993,
            "friedl@example.test",
            "secret",
            self.state,
            1,
            connection_factory=lambda *args, **kwargs: self.imap,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.directory.cleanup()

    def test_first_scan_is_a_baseline_and_only_later_mail_is_announced(self) -> None:
        self.adapter.scan()
        self.assertIsNone(self.state.current())
        self.imap.items[2] = message("New quote", "The quote is R2,000")
        self.adapter.scan()
        event = self.state.current()
        self.assertIsNotNone(event)
        self.assertEqual(event.data["uid"], "2")
        self.assertNotIn("body", event.data)

    async def _next_event(self):
        stream = self.adapter.events()
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    def test_announced_event_carries_body_transiently_without_persisting_it(self) -> None:
        import asyncio

        self.adapter.scan()
        self.imap.items[2] = message("New quote", "The quote is R2,000")
        event = asyncio.run(self._next_event())
        self.assertEqual(event.transient_data["body"], "The quote is R2,000")
        self.assertNotIn("body", event.data)
        retained = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("The quote is R2,000", retained)
        rendered = repr(self.imap.commands)
        self.assertIn("BODY.PEEK[]", rendered)
        self.assertNotIn("STORE", rendered)

    def test_unconfirmed_event_is_offered_again_to_a_new_voice_session(self) -> None:
        import asyncio

        self.adapter.scan()
        self.imap.items[2] = message("Retry me", "Transient body")
        first = asyncio.run(self._next_event())
        second = asyncio.run(self._next_event())
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.transient_data["body"], "Transient body")
        self.assertEqual(second.transient_data["body"], "Transient body")
        self.assertEqual(self.state.current().data["uid"], "2")

    def test_delivered_item_stays_context_without_blocking_later_mail(self) -> None:
        """One at a time, and a presented item remains the referent.

        It must not hold back later mail for good. It is only cleared by
        acknowledging or trashing it, so a message answered in some other way
        would otherwise block every announcement after it permanently.
        """
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = self.state.current()
        self.assertEqual(first.data["uid"], "2")
        self.adapter.record_delivery(first.event_id)
        # Still the referent for "reply to that".
        self.assertEqual(self.adapter.contextual_events()[0].data["subject"], "First")
        # And later mail is still reachable.
        self.assertEqual(self.state.current().data["uid"], "3")
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(self.adapter.contextual_events()[0].data["uid"], "3")
        retained = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("subject", retained)
        self.assertNotIn("sender", retained)

    def test_read_uses_peek_and_trash_is_discovered_then_moved(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("New quote", "The quote is R2,000")
        self.adapter.scan()
        reference = MailReference("INBOX", "777", "2")
        content = self.adapter.read(reference)
        destination = self.adapter.move_to_trash(reference)
        self.assertEqual(content.body, "The quote is R2,000")
        self.assertEqual(destination, "Deleted Messages")
        rendered = repr(self.imap.commands)
        self.assertIn("BODY.PEEK[]", rendered)
        # The Trash mailbox name contains a space, so it must reach IMAP quoted.
        # Passed unquoted the server reads it as two arguments and rejects the
        # command, which is how a real move failed while this double passed.
        self.assertIn("""'MOVE', '2', '"Deleted Messages"'""", rendered)
        self.assertNotIn("STORE", rendered)
        self.assertNotIn("EXPUNGE", rendered)


class MailPrimitiveTests(unittest.TestCase):
    def test_read_body_is_available_to_core_but_excluded_from_durable_values(self) -> None:
        account = FakeAccount()
        current = ["call-1"]
        read = build_mail_executors(account, account, lambda: current[0])[READ_MAIL_MESSAGE]
        result = read({"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"})
        self.assertEqual(result.values["body"], "private body")
        self.assertNotIn("body", result.durable_values)

    def test_acknowledge_and_trash_are_separate_primitive_effects(self) -> None:
        account = FakeAccount()
        current = ["call-1"]
        executors = build_mail_executors(account, account, lambda: current[0])
        arguments = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
        acknowledged = executors[ACKNOWLEDGE_MAIL_MESSAGE](arguments)
        current[0] = "call-2"
        trashed = executors[MOVE_MAIL_MESSAGE_TO_TRASH](arguments)
        self.assertTrue(acknowledged.values["acknowledged"])
        self.assertTrue(trashed.values["moved"])
        self.assertEqual(len(account.acknowledged), 1)
        self.assertEqual(len(account.trashed), 1)
        definitions = {item.capability_id: item for item in DEFINITIONS}
        self.assertEqual(
            definitions[ACKNOWLEDGE_MAIL_MESSAGE].side_effect.value,
            "attention_state",
        )
        self.assertEqual(
            definitions[MOVE_MAIL_MESSAGE_TO_TRASH].side_effect.value,
            "effectful",
        )

    def test_goal_store_never_serializes_transient_mail_body(self) -> None:
        directory = tempfile.TemporaryDirectory()
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        try:
            reference = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
            result = CapabilityResult(
                "call-1",
                READ_MAIL_MESSAGE,
                CapabilityResultState.SUCCEEDED,
                {"reference": reference, "body": "private body"},
                durable_values={"reference": reference},
            )
            state = GoalState(
                "goal-1",
                Objective("turn:turn-1", "Handle mail"),
                (SuccessCriterion("criterion-1", "handled"),),
                attempts=(CapabilityAttempt(
                    CapabilityCall("call-1", READ_MAIL_MESSAGE, reference),
                    CapabilityAttemptDisposition.EXECUTED,
                    True,
                    result,
                ),),
            )
            store.create(state, "conversation-1", RETENTION)
            recovered = store.load("goal-1")
            values = recovered.state.attempts[0].result.values
            self.assertNotIn("body", values)
            self.assertEqual(values["reference"]["uid"], "2")
        finally:
            store.close()
            directory.cleanup()


class BackgroundEventBoundaryTests(unittest.TestCase):
    def test_gateway_keeps_event_transient_and_core_owns_response(self) -> None:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        goals = SQLiteGoalStore(root / "goals.sqlite3")
        reasoner = Queued(AgentDecision(response="A supplier sent a quote."))
        gateway = ConversationGateway(
            CoreAgent(goals, reasoner, lambda call, state: None, ()),
            conversations,
            lambda conversation_id: None,
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
        )
        event = BackgroundEvent(
            "mail:777:2",
            "mail.message_arrived",
            NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
            {"body": "private body"},
        )
        try:
            outcome = gateway.receive_background_event(
                "conversation-1", event, 1, RETENTION
            )
            self.assertEqual(outcome.response, "A supplier sent a quote.")
            self.assertEqual(reasoner.contexts[0].events[0].transient_data["body"], "private body")
            self.assertEqual(reasoner.contexts[0].trigger_event_id, event.event_id)
            recovered = conversations.load("conversation-1")
            self.assertEqual(recovered.events, ())
            self.assertEqual(recovered.turns[-1].origin, ConversationOrigin.ALX_RESPONSE)
        finally:
            conversations.close()
            goals.close()
            directory.cleanup()

    def test_exact_current_turn_approval_is_consumed_by_matching_trash_call(self) -> None:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        goals = SQLiteGoalStore(root / "goals.sqlite3")
        arguments = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
        call = CapabilityCall(
            "call-1", MOVE_MAIL_MESSAGE_TO_TRASH, arguments, "approval-1"
        )
        proposal = ApprovalProposal(
            "approval-1",
            ApprovalScope(MOVE_MAIL_MESSAGE_TO_TRASH, arguments),
            "turn:turn-1",
        )
        reasoner = Queued(
            AgentDecision(
                call=call,
                approval_proposal=proposal,
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE,
                    "Handle the referenced mail",
                    (SuccessCriterion("criterion-1", "requested action verified"),),
                ),
            ),
            AgentDecision(response="Moved to Trash."),
        )

        account = FakeAccount()
        current_call_id = [""]
        registry = CapabilityRegistry(DEFINITIONS)
        broker = CapabilityBroker(
            registry,
            SafetyGate({
                MOVE_MAIL_MESSAGE_TO_TRASH: AuthorityPolicy(
                    frozenset({"mail.trash"}), approval_required=True
                ),
            }),
            build_mail_executors(account, account, lambda: current_call_id[0]),
        )

        def dispatch(issued, state):
            current_call_id[0] = issued.call_id
            self.assertEqual(state.approvals[0].approval_id, "approval-1")
            self.assertEqual(
                state.approvals[0].lifecycle,
                ApprovalLifecycle.GRANTED,
            )
            return broker.dispatch(
                issued,
                AuthorityContext(
                    "friedl",
                    frozenset({"mail.trash"}),
                    NOW,
                    state.approvals,
                ),
            )

        core = CoreAgent(goals, reasoner, dispatch, DEFINITIONS)
        conversation = ConversationSnapshot(
            "conversation-1",
            (ConversationTurn(
                "conversation-1",
                "turn-1",
                ConversationOrigin.SPEECH_TRANSCRIPT,
                "Remove the mail we were discussing",
                NOW,
                "friedl",
            ),),
            1,
            RETENTION,
        )
        try:
            outcome = core.process(conversation, None, RETENTION, 3)
            self.assertEqual(outcome.state, CoreState.RESPONDED)
            self.assertEqual(outcome.snapshot.state.approvals[0].lifecycle.value, "consumed")
            self.assertEqual(
                account.trashed,
                [MailReference("INBOX", "777", "2")],
            )
            self.assertEqual(
                len([
                    item for item in outcome.snapshot.state.attempts
                    if item.call is not None
                    and item.call.capability_id == MOVE_MAIL_MESSAGE_TO_TRASH
                ]),
                1,
            )
        finally:
            goals.close()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
