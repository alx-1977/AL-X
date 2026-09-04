"""Mail handled outside AL/X must not strand her attention.

On 2026-09-04 UID 58781 -- a Woolworths order confirmation -- had been
`presented` for an hour and was blocking every later observation. A direct IMAP
probe showed the message was no longer in INBOX at all: Friedl had cleared it
in a mail client. Three further observations queued behind it were also gone.

The deadlock was structural. `current()` yields nothing while anything is
`presented`, and the only exits -- `acknowledge_mail_message` and a successful
Trash -- both act on a message IMAP could no longer resolve. Nothing
reconciled observation state against the mailbox, so nothing could ever
release it.

Whether a tracked identifier is still in the mailbox has one correct answer, so
detection is deterministic (Law 2). What its disappearance means does not, so
an observation Friedl has already heard about is returned to AL/X as evidence
and she releases it herself (Laws 1 and 3). An observation never announced owes
him nothing and is settled silently.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import MailReference  # noqa: E402
from alx.providers.icloud_mail import (  # noqa: E402
    ICloudMailAdapter, SQLiteMailObservationState,
)
from tests.test_mail_vertical_slice import FakeImap, message  # noqa: E402

VALIDITY = "777"


def observed(uid: int) -> tuple[int, dict[str, str]]:
    return (uid, {
        "mailbox_id": "INBOX", "uid_validity": VALIDITY, "uid": str(uid),
        "observed_at": "2026-09-04T06:00:00+00:00",
        "subject": f"Message {uid}",
    })


class MailReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "mail.sqlite3"
        self.state = SQLiteMailObservationState(self.path)
        self.addCleanup(self.state.close)
        self.state.new_identifiers("INBOX", VALIDITY, ())

    def discover(self, *uids: int) -> None:
        self.state.discover(
            "INBOX", VALIDITY, tuple(observed(uid) for uid in uids), uids
        )

    def states(self) -> dict[int, str]:
        return {
            int(uid): value
            for uid, value in self.state._connection.execute(
                "SELECT uid, state FROM mail_observations"
            )
        }

    def present(self, uid: int) -> None:
        """Put an observation through the real delivery path to `presented`."""
        event = self.state.current()
        self.assertEqual(event.data["uid"], str(uid))
        self.assertTrue(self.state.record_delivery(event.event_id))

    # -- an observation never announced ----------------------------------

    def test_a_pending_observation_that_vanishes_is_settled_silently(self) -> None:
        self.discover(1, 2)
        self.present(1)
        events = self.state.reconcile("INBOX", VALIDITY, (1,))
        self.assertEqual(events, (), "nothing was said about uid 2, so nothing is owed")
        self.assertEqual(self.states()[2], "done")

    def test_settling_a_pending_observation_discards_its_content(self) -> None:
        """Retention: a settled observation keeps references, not headers."""
        self.discover(1, 2)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, (1,))
        stored = self.state._connection.execute(
            "SELECT event_json FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotIn("Message 2", stored)

    # -- an observation she has already raised ---------------------------

    def test_a_presented_observation_that_vanishes_is_returned_to_her(self) -> None:
        self.discover(1)
        self.present(1)
        events = self.state.reconcile("INBOX", VALIDITY, ())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "mail.message_vanished")
        self.assertEqual(events[0].data["uid"], "1")

    def test_reconciliation_does_not_release_what_she_announced(self) -> None:
        """Law 1: code reports the disappearance; only AL/X ends the attention."""
        self.discover(1)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, ())
        self.assertEqual(self.states()[1], "presented")

    def test_she_releases_a_vanished_observation_by_acknowledging_it(self) -> None:
        self.discover(1, 2)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, (2,))
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertEqual(self.states()[1], "done")
        self.assertEqual(
            self.state.current().data["uid"], "2",
            "releasing the ghost lets the queue behind it move",
        )

    # -- reporting exactly once ------------------------------------------

    def test_a_disappearance_is_reported_only_once(self) -> None:
        """Otherwise every 15-second poll spends a reasoning call on it."""
        self.discover(1)
        self.present(1)
        self.assertEqual(len(self.state.reconcile("INBOX", VALIDITY, ())), 1)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), ())
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), ())

    def test_the_report_is_not_repeated_after_a_restart(self) -> None:
        """An in-process guard would forget; a stranded ghost outlives the run."""
        self.discover(1)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, ())
        self.state.close()
        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile("INBOX", VALIDITY, ()), ())
        self.assertEqual(
            restarted.current(), None,
            "and the observation is still hers to release",
        )

    def test_delivering_a_vanished_report_records_no_presentation(self) -> None:
        self.discover(1)
        self.present(1)
        event = self.state.reconcile("INBOX", VALIDITY, ())[0]
        self.assertFalse(self.state.record_delivery(event.event_id))
        self.assertEqual(self.states()[1], "presented")

    # -- what reconciliation must not do ---------------------------------

    def test_an_unscanned_identifier_is_never_treated_as_vanished(self) -> None:
        """Above the cursor nothing has been looked for, so absence means nothing."""
        self.discover(1)
        self.present(1)
        self.state._connection.execute(
            "INSERT INTO mail_observations"
            "(mailbox_id, uid_validity, uid, event_json, state) "
            "VALUES ('INBOX', ?, 9, '{}', 'pending')",
            (VALIDITY,),
        )
        self.state._connection.commit()
        self.state.reconcile("INBOX", VALIDITY, (1,))
        self.assertEqual(self.states()[9], "pending")

    def test_a_message_still_in_the_mailbox_is_left_alone(self) -> None:
        self.discover(1, 2)
        self.present(1)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1, 2)), ())
        self.assertEqual(self.states(), {1: "presented", 2: "pending"})

    def test_reconciliation_does_not_move_the_cursor(self) -> None:
        """Rewinding it would re-announce mail Friedl has already dealt with."""
        self.discover(1, 2)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, ())
        cursor = self.state._connection.execute(
            "SELECT last_uid FROM mail_cursor"
        ).fetchone()[0]
        self.assertEqual(int(cursor), 2)

    def test_a_changed_uid_validity_reconciles_nothing(self) -> None:
        """Identifiers from another generation are not comparable."""
        self.discover(1)
        self.present(1)
        self.assertEqual(self.state.reconcile("INBOX", "999", ()), ())
        self.assertEqual(self.states()[1], "presented")


class BurstContextTest(unittest.TestCase):
    """A burst is answered in one turn, not one interruption per message.

    Mail arrives in bursts that are mostly receipts and notifications. With
    only the held item in context AL/X could not tell whether the next thing
    mattered without announcing it first, so a burst of four became four
    spoken interruptions and four reasoning calls. She is now shown what is
    queued as well as what she holds, and judges the burst in one turn.

    What she may not have is code deciding for her. Filtering the queue by
    sender or subject would be the routing Law 1 forbids, so everything
    waiting is shown and every judgement stays hers.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = SQLiteMailObservationState(
            Path(self.directory.name) / "mail.sqlite3"
        )
        self.addCleanup(self.state.close)
        self.state.new_identifiers("INBOX", VALIDITY, ())
        self.state.discover(
            "INBOX", VALIDITY, tuple(observed(uid) for uid in (1, 2, 3, 4)),
            (1, 2, 3, 4),
        )
        event = self.state.current()
        self.state.record_delivery(event.event_id)

    def kinds(self) -> list[tuple[str, str]]:
        return [(e.kind, e.data["uid"]) for e in self.state.contextual_events()]

    def test_she_sees_what_is_waiting_behind_what_she_holds(self) -> None:
        self.assertEqual(
            self.kinds(),
            [("mail.message_arrived", "1"), ("mail.message_waiting", "4"),
             ("mail.message_waiting", "3"), ("mail.message_waiting", "2")],
        )

    def test_a_waiting_item_carries_its_subject_and_sender(self) -> None:
        """Without them she cannot judge the burst, and code must not judge it."""
        waiting = [
            e for e in self.state.contextual_events()
            if e.kind == "mail.message_waiting"
        ]
        self.assertTrue(all(e.data.get("subject") for e in waiting))

    def test_seeing_a_waiting_item_does_not_announce_it(self) -> None:
        self.state.contextual_events()
        self.state.contextual_events()
        states = dict(
            self.state._connection.execute("SELECT uid, state FROM mail_observations")
        )
        self.assertEqual(states, {1: "presented", 2: "pending", 3: "pending",
                                  4: "pending"})

    def test_she_can_release_a_waiting_item_without_mentioning_it(self) -> None:
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "2"))
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "3"))
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertEqual(
            self.state.current().data["uid"], "4",
            "the queue skips what she already dealt with silently",
        )

    def test_waiting_context_is_bounded(self) -> None:
        """A very large backlog must not become an unbounded context."""
        extra = tuple(range(5, 40))
        self.state.discover(
            "INBOX", VALIDITY, tuple(observed(uid) for uid in extra), extra
        )
        waiting = [
            e for e in self.state.contextual_events()
            if e.kind == "mail.message_waiting"
        ]
        self.assertEqual(len(waiting), self.state.WAITING_EVENT_LIMIT)

    def test_a_settled_observation_is_not_context(self) -> None:
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "2"))
        self.assertNotIn(
            ("mail.message_waiting", "2"), self.kinds(),
        )


class ScanReportsDisappearanceTest(unittest.TestCase):
    """The whole path: a message leaves the mailbox and `scan` reports it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = SQLiteMailObservationState(
            Path(self.directory.name) / "mail.sqlite3"
        )
        self.addCleanup(self.state.close)
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            self.state, 1, connection_factory=lambda *a, **k: self.imap,
        )

    def test_scan_reports_a_presented_message_removed_outside_alx(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks for your order")
        self.adapter.scan()
        event = self.state.current()
        self.assertEqual(event.data["uid"], "2")
        self.state.record_delivery(event.event_id)

        del self.imap.items[2]                      # Friedl deletes it himself
        reported = self.adapter.scan()

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0].kind, "mail.message_vanished")
        self.assertEqual(reported[0].data["uid"], "2")
        self.assertEqual(self.adapter.scan(), (), "and only once")

    def test_scan_still_reports_nothing_when_the_mailbox_is_unchanged(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "body")
        self.assertEqual(self.adapter.scan(), ())


if __name__ == "__main__":
    unittest.main()
