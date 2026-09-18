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
    CognitionOrigin,
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
from alx.bootstrap.reasoning import OriginSelectedReasoner  # noqa: E402
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


def alternative_message(subject: str) -> bytes:
    return (
        f"Message-ID: <{subject}@example.test>\r\n"
        f"Subject: {subject}\r\n"
        "From: Supplier <supplier@example.test>\r\n"
        "Date: Sat, 29 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; boundary=alternative\r\n\r\n"
        "--alternative\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Plain body\r\n"
        "--alternative\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>HTML body</p>\r\n"
        "--alternative--\r\n"
    ).encode()


def inline_image_message(subject: str, *, filename: bool) -> bytes:
    disposition = (
        "Content-Disposition: inline; filename=logo.png\r\n"
        if filename else "Content-Disposition: inline\r\n"
    )
    return (
        f"Message-ID: <{subject}@example.test>\r\n"
        f"Subject: {subject}\r\n"
        "From: Supplier <supplier@example.test>\r\n"
        "Date: Sat, 29 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/related; boundary=related\r\n\r\n"
        "--related\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>Body<img src=\"cid:logo\"></p>\r\n"
        "--related\r\nContent-Type: image/png\r\n"
        "Content-ID: <logo>\r\n"
        f"{disposition}\r\n"
        "image-bytes\r\n--related--\r\n"
    ).encode()


def forwarded_message_with_attachment(subject: str) -> bytes:
    return (
        f"Message-ID: <{subject}@example.test>\r\n"
        f"Subject: {subject}\r\n"
        "From: Supplier <supplier@example.test>\r\n"
        "Date: Sat, 29 Aug 2026 10:00:00 +0200\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
        "--outer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Forwarded message follows\r\n"
        "--outer\r\nContent-Type: message/rfc822\r\n\r\n"
        "From: Other <other@example.test>\r\n"
        "Subject: Original\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=inner\r\n\r\n"
        "--inner\r\nContent-Type: text/plain\r\n\r\nOriginal body\r\n"
        "--inner\r\nContent-Type: application/pdf\r\n"
        "Content-Disposition: attachment; filename=original.pdf\r\n\r\n"
        "PDF\r\n--inner--\r\n"
        "--outer--\r\n"
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


def next_arrival(state):
    """The oldest arrival still awaiting delivery, or None."""
    awaiting = state.unclaimed_arrivals()
    return awaiting[0] if awaiting else None


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
        self.assertIsNone(next_arrival(self.state))
        self.imap.items[2] = message("New quote", "The quote is R2,000")
        self.adapter.scan()
        event = next_arrival(self.state)
        self.assertIsNotNone(event)
        self.assertEqual(event.data["uid"], "2")
        self.assertNotIn("body", event.data)

    def _announced_event(self):
        """The arrival as a turn receives it, body attached.

        Context is where the body is read now. The delivery generator that
        used to attach it lived only as long as a voice session, so a message
        could be observed and never read; building turn context does it for
        every turn instead, whether or not anyone is connected.
        """
        for event in self.adapter.contextual_events():
            if event.kind in ("mail.message_arrived", "mail.message_waiting"):
                return event
        return None

    def test_announced_event_carries_local_metadata_and_no_body(self) -> None:
        """Context names the message; it does not go and fetch it.

        The body used to be attached here, which cost one IMAP connection per
        event while a person waited for the turn to start. What ingestion
        already persisted — sender, subject, identifiers, timestamps — is what
        she needs to judge whether an email matters, and it costs nothing.

        The body is still never persisted. That invariant is older than this
        change and is asserted below unchanged.
        """
        self.adapter.scan()
        self.imap.items[2] = message("New quote", "The quote is R2,000")
        # Discovery is the process poller's job, so reading context only
        # carries what scanning has already made durable.
        self.adapter.scan()
        before = len(self.imap.commands)
        event = self._announced_event()

        # Named as absent rather than missing, so she can tell an empty
        # message from one that has not been fetched.
        self.assertEqual(event.transient_data["content_unavailable"], "not_fetched")
        self.assertNotIn("body", event.transient_data)
        self.assertNotIn("body", event.data)
        # The metadata that makes the event useful is there.
        self.assertEqual(event.data["subject"], "New quote")
        self.assertIn("sender", event.data)
        self.assertIn("observed_at", event.data)
        # Nothing was asked of the mail account to build this.
        self.assertEqual(self.imap.commands[before:], [])

        retained = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("The quote is R2,000", retained)
        self.assertNotIn("STORE", repr(self.imap.commands))

    def test_read_reports_attachment_presence_without_changing_seen(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message_with_attachment("Quote", "Attached quote")
        self.adapter.scan()
        content = self.adapter.read(MailReference("INBOX", "777", "2"))
        self.assertTrue(content.has_attachments)
        self.assertNotIn("STORE", repr(self.imap.commands))

    def _attachment_fact(self, raw_message: bytes) -> bool:
        self.adapter.scan()
        self.imap.items[2] = raw_message
        self.adapter.scan()
        return self.adapter.read(
            MailReference("INBOX", "777", "2")
        ).has_attachments

    def test_plain_text_is_not_an_attachment(self) -> None:
        self.assertFalse(self._attachment_fact(message("Plain", "Plain body")))

    def test_html_alternative_body_is_not_an_attachment(self) -> None:
        """Regression: counting any extra MIME body part silently keeps normal mail."""
        self.assertFalse(self._attachment_fact(alternative_message("Alternative")))

    def test_inline_image_with_filename_is_kept_as_an_attachment(self) -> None:
        self.assertTrue(self._attachment_fact(
            inline_image_message("Named logo", filename=True)
        ))

    def test_inline_image_without_filename_is_not_a_user_attachment(self) -> None:
        self.assertFalse(self._attachment_fact(
            inline_image_message("Unnamed logo", filename=False)
        ))

    def test_forwarded_attachment_is_found_recursively(self) -> None:
        self.assertTrue(self._attachment_fact(
            forwarded_message_with_attachment("Forwarded")
        ))

    def test_mark_seen_sets_only_the_seen_flag(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Handled", "Done")
        self.adapter.scan()
        event = next_arrival(self.state)
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
        # Marking seen is not settling: the observation is still hers to
        # release, so it remains both eligible and in context.
        self.assertEqual(next_arrival(self.state).data["uid"], "2")
        self.assertEqual(self.state.contextual_events()[0].data["uid"], "2")

    def test_mark_seen_failure_is_structured_and_does_not_release_attention(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Handled", "Done")
        self.adapter.scan()
        event = next_arrival(self.state)
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
        event = next_arrival(self.state)
        self.adapter.record_delivery(event.event_id)
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertFalse(any(item[0:2] == ("UID", "STORE")
                             for item in self.imap.commands))

    def test_an_unsettled_observation_is_offered_again(self) -> None:
        """Reading it does not consume it; only settling does.

        The re-offer used to depend on a new voice session asking the delivery
        stream again. It is now a property of the durable observation itself,
        so an unsettled message is still there whether or not anyone connected
        in between, and the ledger is what stops it becoming a second turn.
        """
        self.adapter.scan()
        self.imap.items[2] = message("Retry me", "Transient body")
        # The process poller discovers; reading context carries what is
        # already durable, which is what makes the re-offer possible.
        self.adapter.scan()
        first = self._announced_event()
        second = self._announced_event()
        self.assertEqual(first.event_id, second.event_id)
        # Re-offered identically, and neither offer fetches anything.
        self.assertEqual(first.data["subject"], "Retry me")
        self.assertEqual(second.data["subject"], "Retry me")
        self.assertEqual(
            first.transient_data["content_unavailable"], "not_fetched"
        )
        self.assertEqual(next_arrival(self.state).data["uid"], "2")

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
        current = next_arrival(self.state)
        self.assertEqual(
            current.provenance.mail_references,
            (MailReference("INBOX", "777", "2"),),
        )
        deadline = current.provenance.content_expires_at
        self.state.close()
        self.state = SQLiteMailObservationState(self.path)
        recovered = next_arrival(self.state)
        self.assertEqual(recovered.provenance.content_expires_at, deadline)

    def test_a_burst_is_reported_whole_rather_than_one_at_a_time(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Promotion", "Promo body")
        self.imap.items[3] = message("Parts order", "When do the parts arrive?")
        self.adapter.scan()
        first = next_arrival(self.state)
        self.adapter.record_delivery(first.event_id)
        # The store no longer holds one observation at a time: exactly-once is
        # the opportunity ledger's job, so both are reported and the ledger
        # decides which has already been taken. A delivered item stays until
        # she settles it, because delivery is not the same as being finished.
        self.assertEqual(
            [item.data["subject"] for item in self.state.unclaimed_arrivals()],
            ["Promotion", "Parts order"],
        )
        # She still sees what she is holding and what is waiting behind it.
        self.assertEqual(
            [
                (item.kind, item.data["subject"])
                for item in self.adapter.contextual_events()
            ],
            [
                ("mail.message_arrived", "Promotion"),
                ("mail.message_waiting", "Parts order"),
            ],
        )
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(next_arrival(self.state).data["subject"], "Parts order")

    def test_contextual_events_stay_bounded(self) -> None:
        from alx.providers import SQLiteMailObservationState

        self.adapter.scan()
        for uid in range(2, 12):
            self.imap.items[uid] = message(f"Subject {uid}", "body")
        self.adapter.scan()
        for _ in range(10):
            item = next_arrival(self.state)
            if item is None:
                break
            self.adapter.record_delivery(item.event_id)
        events = self.adapter.contextual_events()
        announced = [e for e in events if e.kind == "mail.message_arrived"]
        waiting = [e for e in events if e.kind == "mail.message_waiting"]
        self.assertLessEqual(
            len(announced), SQLiteMailObservationState.CONTEXTUAL_EVENT_LIMIT
        )
        self.assertLessEqual(
            len(waiting), SQLiteMailObservationState.WAITING_EVENT_LIMIT
        )

    def test_a_delivered_item_stays_context_without_holding_the_queue(self) -> None:
        """Delivery confirmation does not mean Friedl finished with the mail."""
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = next_arrival(self.state)
        self.assertEqual(first.data["uid"], "2")
        self.adapter.record_delivery(first.event_id)
        # Still the referent for "reply to that".
        self.assertEqual(self.adapter.contextual_events()[0].data["subject"], "First")
        # And the later item is reported alongside it rather than held behind.
        self.assertEqual(
            [item.data["uid"] for item in self.state.unclaimed_arrivals()],
            ["2", "3"],
        )
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(next_arrival(self.state).data["uid"], "3")
        retained = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("subject", retained)
        self.assertNotIn("sender", retained)

    def test_a_presented_item_survives_restart_as_context(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = next_arrival(self.state)
        self.adapter.record_delivery(first.event_id)
        self.state.close()
        self.state = SQLiteMailObservationState(self.path)
        self.assertEqual(
            self.state.contextual_events()[0].data["subject"], "First"
        )
        pending = self.state._connection.execute(
            "SELECT COUNT(*) FROM mail_observations WHERE state = 'pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)

    def test_a_legacy_current_row_is_still_deliverable(self) -> None:
        """Upgrading must not strand a row an earlier runtime had promoted."""
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = next_arrival(self.state)
        self.adapter.record_delivery(first.event_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE mail_observations SET state = 'current' WHERE uid = 3"
            )
        # A row an earlier runtime promoted is reported, not stranded.
        self.assertIn(
            "Second",
            [item.data["subject"] for item in self.state.unclaimed_arrivals()],
        )
        self.adapter.acknowledge(MailReference("INBOX", "777", "2"))
        self.assertEqual(next_arrival(self.state).data["subject"], "Second")

    def test_successful_trash_releases_the_next_item(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("First", "First body")
        self.imap.items[3] = message("Second", "Second body")
        self.adapter.scan()
        first = next_arrival(self.state)
        self.adapter.record_delivery(first.event_id)
        trash = self.adapter.move_to_trash(MailReference("INBOX", "777", "2"))
        self.assertEqual(trash, "Deleted Messages")
        self.assertEqual(next_arrival(self.state).data["subject"], "Second")

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
                    "friedl@example.test", "secret", "imap.example.test", 993, 15, ""
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


def mail_occasion(event, conversation_id="conversation-1"):
    """The occasion an observed message raises, as MailCognitionSource makes it."""
    from alx.contracts import CognitionOpportunity
    from alx.continuity.mail_source import MailCognitionSource

    return CognitionOpportunity(
        opportunity_id=MailCognitionSource.opportunity_id_for(event.event_id),
        origin=CognitionOrigin.EXTERNAL_EVENT,
        arose_at=event.occurred_at,
        conversation_id=conversation_id,
        references=(f"mail_observation:{event.event_id}",),
        provenance=event.provenance,
    )


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
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
            contextual_events=lambda: (event,),
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
            outcome = gateway.receive_cognition_opportunity(
                "conversation-1", mail_occasion(event), 1, RETENTION
            )
            self.assertEqual(outcome.response, "A supplier sent a quote.")
            # The observation reaches the turn as context, carrying the body
            # transiently. The occasion itself is a separate event beside it.
            observed = [
                item for item in reasoner.contexts[0].events
                if item.kind == "mail.message_arrived"
            ]
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0].transient_data["body"], "private body")
            # The trigger is the occasion, which is a different identity from
            # the observation so that both survive the gateway's event merge.
            self.assertEqual(
                reasoner.contexts[0].trigger_event_id,
                mail_occasion(event).opportunity_id,
            )
            self.assertIs(reasoner.contexts[0].origin, CognitionOrigin.EXTERNAL_EVENT)
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

    def test_mail_event_selects_the_external_reasoning_path(self) -> None:
        """Mail provenance, not its content, selects the autonomous Core."""
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        goals = SQLiteGoalStore(root / "goals.sqlite3")

        class RecordingReasoner:
            def __init__(self) -> None:
                self.contexts = []

            def decide(self, context):
                self.contexts.append(context)
                return AgentDecision(response="Observed.")

        person = RecordingReasoner()
        external = RecordingReasoner()
        gateway = ConversationGateway(
            CoreAgent(
                goals, OriginSelectedReasoner(person, external),
                lambda call, state: None, (),
            ),
            conversations,
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
            contextual_events=lambda: (event,),
        )
        event = BackgroundEvent(
            "mail:777:2", "mail.message_arrived", NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        try:
            gateway.receive_cognition_opportunity(
                "conversation-1", mail_occasion(event), 1, RETENTION
            )
            self.assertEqual(person.contexts, [])
            self.assertEqual(len(external.contexts), 1)
            self.assertIs(external.contexts[0].origin, CognitionOrigin.EXTERNAL_EVENT)
            self.assertEqual(
                external.contexts[0].trigger_event_id,
                mail_occasion(event).opportunity_id,
            )
        finally:
            conversations.close()
            goals.close()
            directory.cleanup()

    def test_disabled_autonomous_mail_is_silently_handled_without_a_model_call(self) -> None:
        """A disabled autonomous slot is not a conversational fallback or error."""
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        goals = SQLiteGoalStore(root / "goals.sqlite3")

        class PersonReasoner:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, context):
                self.calls += 1
                raise AssertionError("external mail must not reach the person reasoner")

        person = PersonReasoner()
        gateway = ConversationGateway(
            CoreAgent(
                goals, OriginSelectedReasoner(person, None),
                lambda call, state: None, (),
            ),
            conversations,
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
            contextual_events=lambda: (event,),
        )
        event = BackgroundEvent(
            "mail:777:2", "mail.message_arrived", NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        try:
            result = gateway.receive_cognition_opportunity(
                "conversation-1", mail_occasion(event), 1, RETENTION
            )
            self.assertIs(result.state, CoreState.FINISHED_SILENTLY)
            self.assertEqual(result.reason, "autonomous_reasoning_disabled")
            self.assertEqual(person.calls, 0)
            self.assertEqual(conversations.load("conversation-1").turns, ())
        finally:
            conversations.close()
            goals.close()
            directory.cleanup()

    def test_enabled_autonomous_reasoner_failure_remains_an_error(self) -> None:
        """Only deliberate disabled configuration receives the silent outcome."""
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        goals = SQLiteGoalStore(root / "goals.sqlite3")

        class PersonReasoner:
            def decide(self, context):
                raise AssertionError("external mail must not reach the person reasoner")

        class FailingAutonomousReasoner:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, context):
                self.calls += 1
                raise RuntimeError("provider failed")

        autonomous = FailingAutonomousReasoner()
        gateway = ConversationGateway(
            CoreAgent(
                goals, OriginSelectedReasoner(PersonReasoner(), autonomous),
                lambda call, state: None, (),
            ),
            conversations,
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
            contextual_events=lambda: (event,),
        )
        event = BackgroundEvent(
            "mail:777:2", "mail.message_arrived", NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        try:
            result = gateway.receive_cognition_opportunity(
                "conversation-1", mail_occasion(event), 1, RETENTION
            )
            self.assertIs(result.state, CoreState.ERROR)
            self.assertEqual(result.reason, "reasoner_error")
            self.assertEqual(autonomous.calls, 1)
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
            outcome = core.process(conversation, RETENTION, 3)
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

class ForegroundContextIsLocalTests(unittest.TestCase):
    """Preparing a turn must not depend on the mail account answering.

    On 2026-09-18 a typed greeting took ninety-two seconds before the
    reasoning provider was invoked. Eight mail observations were waiting, and
    context assembly attached each message body by reading it: one IMAP
    connection per event — connect, TLS, LOGIN, SELECT, FETCH, LOGOUT — in
    series, on the path a person waits on. Nothing failed and nothing was
    logged; it was merely slow, so the interface showed "reasoning in
    progress" throughout.

    The cost was proportional to the mail queue, which is the wrong thing for
    a greeting to depend on. These tests hold context assembly to local state,
    and the count assertions are what stop the fetch being reintroduced.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = SQLiteMailObservationState(
            Path(self.directory.name) / "observations.sqlite3"
        )
        self.addCleanup(self.state.close)
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            self.state, 1,
            connection_factory=lambda *arguments, **keywords: self.imap,
        )

    def observe(self, count: int) -> None:
        """Put `count` observations in local state, as scanning would."""
        self.adapter.scan()
        for index in range(2, 2 + count):
            self.imap.items[index] = message(
                f"Subject {index}", f"Body {index}"
            )
        self.adapter.scan()

    def test_context_assembly_opens_no_connection_and_sends_no_command(
        self,
    ) -> None:
        self.observe(8)
        before = len(self.imap.commands)
        events = self.adapter.contextual_events()
        self.assertTrue(events, "the observations should reach context")
        self.assertEqual(
            self.imap.commands[before:],
            [],
            "context assembly must not talk to the mail account",
        )

    def test_the_cost_does_not_grow_with_the_number_waiting(self) -> None:
        """Zero, eight or eighty: the same number of commands, which is none."""
        for count in (0, 1, 8, 20):
            with self.subTest(waiting=count):
                self.setUp()
                self.observe(count)
                before = len(self.imap.commands)
                self.adapter.contextual_events()
                self.assertEqual(len(self.imap.commands) - before, 0)

    def test_no_event_is_read_through_the_network_path(self) -> None:
        """Structural: the fetching helper is not reached from here.

        The count assertions above would also catch a reintroduced fetch, but
        this names the specific call that caused the incident.
        """
        self.observe(4)
        calls: list[object] = []
        original = type(self.adapter).read_transient
        type(self.adapter).read_transient = (
            lambda self, event: calls.append(event) or original(self, event)
        )
        self.addCleanup(
            setattr, type(self.adapter), "read_transient", original
        )
        self.adapter.contextual_events()
        self.assertEqual(calls, [])

    def test_useful_local_metadata_still_reaches_the_core(self) -> None:
        """What she needs to judge an email is what ingestion already stored."""
        self.observe(1)
        event = next(
            item
            for item in self.adapter.contextual_events()
            if item.kind in ("mail.message_arrived", "mail.message_waiting")
        )
        self.assertEqual(event.data["subject"], "Subject 2")
        self.assertIn("sender", event.data)
        self.assertIn("observed_at", event.data)
        self.assertIn("message_id", event.data)
        self.assertIn("uid", event.data)

    def test_an_absent_body_is_named_rather_than_omitted(self) -> None:
        """A message with no text and one not fetched are different facts."""
        self.observe(1)
        event = next(
            item
            for item in self.adapter.contextual_events()
            if item.kind in ("mail.message_arrived", "mail.message_waiting")
        )
        self.assertEqual(
            event.transient_data["content_unavailable"], "not_fetched"
        )
        self.assertNotIn("body", event.transient_data)

    def test_reading_a_message_deliberately_still_fetches_it(self) -> None:
        """The capability she calls is unchanged: that is where I/O belongs."""
        self.observe(1)
        before = len(self.imap.commands)
        content = self.adapter.read(MailReference("INBOX", "777", "2"))
        self.assertEqual(content.body, "Body 2")
        self.assertIn("BODY.PEEK[]", repr(self.imap.commands[before:]))

    def test_the_gateway_assembles_context_with_the_account_unreachable(
        self,
    ) -> None:
        """The production path, with every connection attempt refused.

        If anything on this path still reached for the network, the turn would
        fail here rather than merely be slow.
        """
        self.observe(6)

        def refuse(*arguments, **keywords):
            raise AssertionError("no connection may be opened while assembling")

        self.adapter._connection_factory = refuse
        events = self.adapter.contextual_events()
        self.assertTrue(events)
        for event in events:
            if event.kind in ("mail.message_arrived", "mail.message_waiting"):
                self.assertEqual(
                    event.transient_data["content_unavailable"], "not_fetched"
                )


if __name__ == "__main__":
    unittest.main()
