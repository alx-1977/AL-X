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
    RetentionPolicy,
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
    MARK_MAIL_MESSAGE_SEEN,
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


def message_with_attachment(subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{subject}@example.test>\r\n"
        f"Subject: {subject}\r\n"
        "From: Supplier <supplier@example.test>\r\n"
        "Date: Sat, 29 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        "--part\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}\r\n"
        "--part\r\nContent-Type: application/pdf\r\n"
        "Content-Disposition: attachment; filename=quote.pdf\r\n\r\n"
        "PDF\r\n--part--\r\n"
    ).encode()


class FakeImap:
    def __init__(self) -> None:
        self.items = {1: message("Old", "Old body")}
        self.commands = []
        self.store_status = "OK"

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
        if operation == "STORE":
            return self.store_status, [b""]
        raise AssertionError(operation)

    def list(self):
        return "OK", [b'(\\Trash) "/" "Deleted Messages"']


class FakeAccount:
    def __init__(self) -> None:
        self.acknowledged = []
        self.seen = []
        self.trashed = []

    def read(self, reference):
        return MailContent(reference, "Quote", "Supplier", "today", "private body")

    def acknowledge(self, reference):
        self.acknowledged.append(reference)

    def mark_seen(self, reference):
        self.seen.append(reference)

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
        self.path = Path(self.directory.name) / "observations.sqlite3"
        self.state = SQLiteMailObservationState(self.path)
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

    def test_read_reports_attachment_presence_without_changing_seen(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message_with_attachment("Quote", "Attached quote")
        self.adapter.scan()
        content = self.adapter.read(MailReference("INBOX", "777", "2"))
        self.assertTrue(content.has_attachments)
        self.assertNotIn("STORE", repr(self.imap.commands))

    def test_mark_seen_sets_only_the_seen_flag(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Handled", "Done")
        self.adapter.scan()
        event = self.state.current()
        self.adapter.record_delivery(event.event_id)
        self.adapter.mark_seen(MailReference("INBOX", "777", "2"))
        stores = [item for item in self.imap.commands
                  if item[0:2] == ("UID", "STORE")]
        self.assertEqual(
            stores,
            [("UID", "STORE", "2", "+FLAGS.SILENT", r"(\Seen)")],
        )
        self.assertFalse(any(item[0:2] == ("UID", "MOVE")
                             for item in self.imap.commands))
        self.assertIsNone(self.state.current())
        self.assertEqual(self.state.contextual_events()[0].data["uid"], "2")

    def test_mark_seen_failure_is_structured_and_does_not_release_attention(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Handled", "Done")
        self.adapter.scan()
        event = self.state.current()
        self.adapter.record_delivery(event.event_id)
        self.imap.store_status = "NO"
        current = ["seen-1"]
        result = build_mail_executors(
            self.adapter, self.adapter, lambda: current[0]
        )[MARK_MAIL_MESSAGE_SEEN]({
            "mailbox_id": "INBOX", "uid_validity": "777", "uid": "2",
        })
        self.assertEqual(result.failure["code"], "flag_update_failed")
        self.assertEqual(self.state.contextual_events()[0].data["uid"], "2")

    def test_local_dismissal_leaves_the_message_unseen(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Later", "Come back to this")
        self.adapter.scan()
        event = self.state.current()
        self.adapter.record_delivery(event.event_id)
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertFalse(any(item[0:2] == ("UID", "STORE")
                             for item in self.imap.commands))

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

    def test_the_cursor_advances_across_non_contiguous_identifiers(self) -> None:
        """Regression: the cursor required the next identifier to be last + 1.

        IMAP identifiers increase but need not be contiguous, so a permanent
        gap left by a deleted message stalled the cursor and every later
        message was fetched again on every scan. The previous test encoded that
        same contiguity assumption.
        """
        self.adapter.scan()
        event = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "3",
                 "observed_at": "2026-08-30T10:00:00+00:00"}
        self.state.discover("INBOX", "777", ((3, event),), (3,))
        self.assertEqual(self._cursor(), 3)

    def test_the_cursor_stops_before_an_identifier_that_failed(self) -> None:
        """A message whose headers failed this scan must be retried."""
        self.adapter.scan()
        event = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "7",
                 "observed_at": "2026-08-30T10:00:00+00:00"}
        # 5 was attempted and failed; 7 succeeded.
        self.state.discover("INBOX", "777", ((7, event),), (5, 7))
        self.assertLess(self._cursor(), 5, "the failed message must be retried")
        # Once it succeeds the cursor moves past both.
        recovered = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "5",
                     "observed_at": "2026-08-30T10:01:00+00:00"}
        self.state.discover("INBOX", "777", ((5, recovered), (7, event)), (5, 7))
        self.assertEqual(self._cursor(), 7)

    def _cursor(self) -> int:
        return self.state._connection.execute(
            "SELECT last_uid FROM mail_cursor WHERE mailbox_id = 'INBOX'"
        ).fetchone()[0]

    def test_observation_restart_recovers_mail_provenance(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Provenance", "private body")
        self.adapter.scan()
        current = self.state.current()
        self.assertEqual(
            current.provenance.mail_references,
            (MailReference("INBOX", "777", "2"),),
        )
        deadline = current.provenance.content_expires_at
        self.state.close()
        self.state = SQLiteMailObservationState(self.path)
        recovered = self.state.current()
        self.assertEqual(recovered.provenance.content_expires_at, deadline)

    def test_later_mail_stays_queued_until_the_presented_item_is_released(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Promotion", "Promo body")
        self.imap.items[3] = message("Parts order", "When do the parts arrive?")
        self.adapter.scan()
        first = self.state.current()
        self.adapter.record_delivery(first.event_id)
        self.assertIsNone(self.state.current())
        self.assertEqual(
            [item.data["subject"] for item in self.adapter.contextual_events()],
            ["Promotion"],
        )
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        second = self.state.current()
        self.assertEqual(second.data["subject"], "Parts order")

    def test_contextual_events_stay_bounded(self) -> None:
        from alx.providers import SQLiteMailObservationState

        self.adapter.scan()
        for uid in range(2, 12):
            self.imap.items[uid] = message(f"Subject {uid}", "body")
        self.adapter.scan()
        for _ in range(10):
            item = self.state.current()
            if item is None:
                break
            self.adapter.record_delivery(item.event_id)
        self.assertLessEqual(
            len(self.adapter.contextual_events()),
            SQLiteMailObservationState.CONTEXTUAL_EVENT_LIMIT,
        )

    def test_delivered_item_stays_context_and_blocks_later_delivery(self) -> None:
        """Delivery confirmation does not mean Friedl finished with the mail."""
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = self.state.current()
        self.assertEqual(first.data["uid"], "2")
        self.adapter.record_delivery(first.event_id)
        # Still the referent for "reply to that".
        self.assertEqual(self.adapter.contextual_events()[0].data["subject"], "First")
        # The later item remains queued rather than being announced back-to-back.
        self.assertIsNone(self.state.current())
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(self.state.current().data["uid"], "3")
        retained = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("subject", retained)
        self.assertNotIn("sender", retained)

    def test_presented_item_still_blocks_after_restart(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = self.state.current()
        self.adapter.record_delivery(first.event_id)
        self.state.close()
        self.state = SQLiteMailObservationState(self.path)
        self.assertIsNone(self.state.current())
        self.assertEqual(
            self.state.contextual_events()[0].data["subject"], "First"
        )
        pending = self.state._connection.execute(
            "SELECT COUNT(*) FROM mail_observations WHERE state = 'pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)

    def test_legacy_presented_item_blocks_an_already_current_later_item(self) -> None:
        """Upgrading must not announce a pre-promoted item behind an open one."""
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = self.state.current()
        self.adapter.record_delivery(first.event_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE mail_observations SET state = 'current' WHERE uid = 3"
            )
        self.assertIsNone(self.state.current())
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(self.state.current().data["subject"], "Second")

    def test_successful_trash_releases_the_next_item(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = self.state.current()
        self.adapter.record_delivery(first.event_id)
        trash = self.adapter.move_to_trash(MailReference("INBOX", "777", "2"))
        self.assertEqual(trash, "Deleted Messages")
        self.assertEqual(self.state.current().data["subject"], "Second")

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
        self.assertEqual(result.provenance.mail_references, (MailReference("INBOX", "777", "2"),))
        self.assertEqual(
            result.provenance.content_expires_at,
            result.provenance.recorded_at + timedelta(days=30),
        )

    def test_acknowledge_and_trash_are_separate_primitive_effects(self) -> None:
        account = FakeAccount()
        current = ["call-1"]
        executors = build_mail_executors(account, account, lambda: current[0])
        arguments = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
        acknowledged = executors[ACKNOWLEDGE_MAIL_MESSAGE](arguments)
        current[0] = "call-seen"
        seen = executors[MARK_MAIL_MESSAGE_SEEN](arguments)
        current[0] = "call-2"
        trashed = executors[MOVE_MAIL_MESSAGE_TO_TRASH](arguments)
        self.assertTrue(acknowledged.values["acknowledged"])
        self.assertTrue(seen.values["seen"])
        self.assertTrue(trashed.values["moved"])
        self.assertEqual(len(account.acknowledged), 1)
        self.assertEqual(len(account.seen), 1)
        self.assertEqual(len(account.trashed), 1)
        definitions = {item.capability_id: item for item in DEFINITIONS}
        self.assertEqual(
            definitions[ACKNOWLEDGE_MAIL_MESSAGE].side_effect.value,
            "attention_state",
        )
        self.assertIn(
            "changes no mail item or Seen/Unseen state",
            definitions[ACKNOWLEDGE_MAIL_MESSAGE].purpose,
        )
        self.assertEqual(
            definitions[MOVE_MAIL_MESSAGE_TO_TRASH].side_effect.value,
            "effectful",
        )

    def test_seen_and_trash_keep_approval_and_allow_only_exact_standing_scopes(self) -> None:
        from alx.bootstrap.mail import build_mail_runtime
        from alx.config import MailSettings

        directory = tempfile.TemporaryDirectory()
        try:
            runtime = build_mail_runtime(
                MailSettings(
                    "friedl@example.test", "secret", "imap.example.test", 993, 15
                ),
                Path(directory.name),
                lambda: "call-1",
            )
            for capability_id in (
                MARK_MAIL_MESSAGE_SEEN, MOVE_MAIL_MESSAGE_TO_TRASH,
            ):
                policy = runtime.policies[capability_id]
                self.assertTrue(policy.approval_required)
                self.assertTrue(policy.standing_scope_allowed)
            runtime.observations.close()
        finally:
            directory.cleanup()

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
            RetentionPolicy().direct_mail(
                NOW, (MailReference("INBOX", "777", "2"),)
            ),
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
            self.assertEqual(
                recovered.turns[-1].provenance.mail_references,
                (MailReference("INBOX", "777", "2"),),
            )
            self.assertEqual(
                recovered.turns[-1].provenance.content_expires_at,
                NOW + timedelta(days=30),
            )
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
