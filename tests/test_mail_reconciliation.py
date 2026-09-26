"""Mail handled outside AL/X must not strand her attention.

On 2026-09-04 UID 58781 -- a Woolworths order confirmation -- had been
`presented` for an hour and was blocking every later observation. A direct IMAP
probe showed the message was no longer in INBOX at all: Friedl had cleared it
in a mail client. Three further observations queued behind it were also gone.

The deadlock was structural. The single-slot reader yielded nothing while
anything was `presented`, and the only exits -- `acknowledge_mail_message` and
a successful Trash -- both acted on a message IMAP could no longer resolve.
Nothing reconciled observation state against the mailbox, so nothing could
ever release it.

That slot is gone: exactly-once is the shared opportunity ledger's job now, and
the store reports the whole queue. Reconciliation still matters for exactly the
reason it always did -- a message handled elsewhere must not strand her -- so
these tests keep proving it against the reader that replaced it.

Whether a tracked identifier is still in the mailbox has one correct answer, so
detection is deterministic (Law 2). What its disappearance means does not, so
an observation she is holding — current or presented — is returned to AL/X as
evidence and she releases it herself (Laws 1 and 3). A pending observation,
shown as waiting or not, owes no vanished report and is settled silently.
"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.contracts import MailReference  # noqa: E402
from alx.providers.icloud_mail import (  # noqa: E402
    ICloudMailAdapter, SQLiteMailObservationState,
)
from test_mail_vertical_slice import FakeImap, message  # noqa: E402

VALIDITY = "777"


def next_arrival(state):
    """The oldest arrival still awaiting delivery, or None.

    What the removed single-slot reader returned, expressed over the reader
    that replaced it. The store no longer holds one observation at a time, so
    "the next one" is a question the caller asks rather than a state the store
    keeps; these tests ask it because the properties they prove -- that a
    vanished message is not offered as an arrival, that a settled one is not
    re-offered -- are about which observations are eligible at all.
    """
    awaiting = state.unclaimed_arrivals()
    return awaiting[0] if awaiting else None


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
        event = next_arrival(self.state)
        self.assertEqual(event.data["uid"], str(uid))
        self.assertTrue(self.state.record_delivery(event.event_id))

    # -- an observation never announced ----------------------------------

    def test_a_pending_observation_that_vanishes_is_settled_silently(self) -> None:
        self.discover(1, 2)
        self.present(1)
        self.assertEqual(
            self.state.reconcile("INBOX", VALIDITY, (1,)), 0,
            "nothing was said about uid 2, so nothing is owed",
        )
        self.assertEqual(self.states()[2], "done")
        self.assertEqual(self.state.pending_vanished(), ())

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
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 1)
        events = self.state.pending_vanished()
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
            next_arrival(self.state).data["uid"], "2",
            "releasing the ghost lets the queue behind it move",
        )

    # -- reporting exactly once ------------------------------------------

    def test_a_disappearance_is_reported_only_once(self) -> None:
        """Otherwise every 15-second poll spends a reasoning call on it."""
        self.discover(1)
        self.present(1)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 1)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 0)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 0)
        self.assertEqual(
            len(self.state.pending_vanished()), 1,
            "detected once, and still awaiting delivery",
        )

    def test_the_report_is_not_repeated_after_a_restart(self) -> None:
        """An in-process guard would forget; a stranded ghost outlives the run."""
        self.discover(1)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, ())
        self.state.close()
        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile("INBOX", VALIDITY, ()), 0)
        self.assertEqual(
            len(restarted.pending_vanished()), 1,
            "found before the restart, still undelivered, so still carried",
        )
        self.assertEqual(
            next_arrival(restarted), None,
            "and the observation is still hers to release",
        )

    def test_delivering_a_vanished_report_records_no_presentation(self) -> None:
        self.discover(1)
        self.present(1)
        self.state.reconcile("INBOX", VALIDITY, ())
        event = self.state.pending_vanished()[0]
        self.assertFalse(self.state.record_delivery(event.event_id))
        self.assertEqual(self.states()[1], "presented")
        self.assertEqual(
            self.state.pending_vanished(), (),
            "carried once; a later session does not repeat it",
        )

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
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1, 2)), 0)
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
        self.assertEqual(self.state.reconcile("INBOX", "999", ()), 0)
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
        event = next_arrival(self.state)
        self.state.record_delivery(event.event_id)

    def kinds(self) -> list[tuple[str, str]]:
        return [(e.kind, e.data["uid"]) for e in self.state.contextual_events()]

    def test_she_sees_what_is_waiting_behind_what_she_holds(self) -> None:
        """Waiting is shown in delivery order, oldest first."""
        self.assertEqual(
            self.kinds(),
            [("mail.message_arrived", "1"), ("mail.message_waiting", "2"),
             ("mail.message_waiting", "3"), ("mail.message_waiting", "4")],
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
            next_arrival(self.state).data["uid"], "4",
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
        event = next_arrival(self.state)
        self.assertEqual(event.data["uid"], "2")
        self.state.record_delivery(event.event_id)

        del self.imap.items[2]                      # Friedl deletes it himself
        self.adapter.scan()

        reported = self.state.pending_vanished()
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0].kind, "mail.message_vanished")
        self.assertEqual(reported[0].data["uid"], "2")
        self.adapter.scan()
        self.assertEqual(
            len(self.state.pending_vanished()), 1,
            "found once; scanning again neither repeats nor loses it",
        )

    def test_scan_finds_nothing_vanished_when_the_mailbox_is_unchanged(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "body")
        self.adapter.scan()
        self.assertEqual(self.state.pending_vanished(), ())

    def test_scan_settles_a_pending_message_marked_seen_outside_alx(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "body")
        self.adapter.scan()
        self.assertEqual(next_arrival(self.state).data["uid"], "2")
        self.imap.seen_uids.add(2)
        self.adapter.scan()
        self.assertIsNone(next_arrival(self.state))
        self.assertEqual(self.state.pending_vanished(), ())

    def test_scan_does_not_offer_a_new_message_already_seen(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Already read", "body")
        self.imap.seen_uids.add(2)
        self.adapter.scan()
        self.assertIsNone(next_arrival(self.state))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT state FROM mail_observations WHERE uid = 2"
            ).fetchone()[0],
            "done",
        )

    def test_seen_row_above_cursor_settles_after_earlier_fetch_failure(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = message("Fetch later", "body")
        self.imap.items[3] = message("Already read", "body")
        self.imap.fetch_failures.add(2)
        self.imap.seen_uids.add(3)
        self.adapter.scan()
        self.assertIsNone(next_arrival(self.state))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT state FROM mail_observations WHERE uid = 3"
            ).fetchone()[0],
            "done",
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT last_uid FROM mail_cursor WHERE mailbox_id = 'INBOX'"
            ).fetchone()[0],
            1,
        )




class VanishedIsNeverAnArrivalTest(unittest.TestCase):
    """A message found gone is never offered to AL/X as a new arrival.

    On 2026-09-05 she told Friedl about mail that was not in his inbox, and
    then could not delete it because it did not exist. Her reasoning was
    right at every step; the provider had handed her two facts about one
    message — that it was gone, and that it had arrived — and she met the
    first with silence because she had never mentioned it.

    A pending message, including one already shown as waiting, is settled to
    done when it leaves. That is neither a vanished report nor an arrival. A
    presented message that leaves is reported vanished once and is still not
    offered as an arrival. A row already marked vanished is withheld from the
    arrival selector, so the two facts are never both offered.
    """

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

    def test_a_message_that_vanishes_before_she_speaks_is_not_announced(self) -> None:
        """The reported bug, end to end.

        Shown to the Core as waiting, then deleted from the mailbox before the
        turn that would have raised it. The pending row is settled with no
        vanished report, and she must never afterwards be handed it as though
        it had just arrived.
        """
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks for your order")
        self.adapter.scan()

        self.state.contextual_events()               # shown, not yet spoken
        arrival = next_arrival(self.state)
        self.assertEqual(arrival.data["uid"], "2")

        del self.imap.items[2]                       # Friedl deletes it
        self.adapter.scan()

        self.assertEqual(self.state.pending_vanished(), ())
        self.assertIsNone(
            next_arrival(self.state),
            "a message known to be gone was offered as a new arrival",
        )
        state = self.state._connection.execute(
            "SELECT state FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertEqual(state, "done")

    def test_a_waiting_absence_is_settled_without_a_vanished_event(self) -> None:
        """A still-pending message that leaves is a silent release."""
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks")
        self.adapter.scan()
        self.state.contextual_events()
        del self.imap.items[2]
        self.adapter.scan()

        self.assertIsNone(next_arrival(self.state))
        self.assertEqual(self.state.pending_vanished(), ())
        state = self.state._connection.execute(
            "SELECT state FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertEqual(state, "done")

    def test_a_vanished_pending_message_is_never_promoted(self) -> None:
        """It disappears before the transport ever picks it up."""
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks")
        self.adapter.scan()
        del self.imap.items[2]
        self.adapter.scan()

        self.assertIsNone(next_arrival(self.state))

    def test_no_observation_satisfies_both_selectors(self) -> None:
        """The invariant itself, whatever the state.

        A row that is both 'an arrival to raise' and 'a disappearance to
        report' is the shape of the bug, so it is asserted directly rather
        than only through the sequence that produced it.
        """
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks")
        self.adapter.scan()
        self.state.contextual_events()
        del self.imap.items[2]
        self.adapter.scan()

        vanished = {item.data["uid"] for item in self.state.pending_vanished()}
        arrival = next_arrival(self.state)
        self.assertEqual(vanished, set())
        self.assertIsNone(arrival)

    def test_an_ordinary_arrival_is_unaffected(self) -> None:
        """The narrowing must not withhold mail that is genuinely there."""
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks")
        self.adapter.scan()

        arrival = next_arrival(self.state)
        self.assertIsNotNone(arrival, "a present message was withheld")
        self.assertEqual(arrival.data["uid"], "2")
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_later_message_still_arrives_after_one_vanishes(self) -> None:
        """One disappearance must not block the mail behind it."""
        self.adapter.scan()
        self.imap.items[2] = message("First", "gone soon")
        self.adapter.scan()
        self.state.contextual_events()
        del self.imap.items[2]
        self.adapter.scan()
        self.assertEqual(self.state.pending_vanished(), ())

        self.imap.items[3] = message("Second", "still here")
        self.adapter.scan()
        arrival = next_arrival(self.state)
        self.assertIsNotNone(arrival, "the next message never arrived")
        self.assertEqual(arrival.data["uid"], "3")

    def test_a_pending_row_marked_vanished_is_never_promoted(self) -> None:
        """The narrow race the second guard exists for.

        `reconcile` settles a `pending` row straight to `done`, so this state
        is not reachable through the ordinary sequence. It is reachable when a
        message is found gone between the read that selects a pending row and
        the write that promotes it. Constructed directly, because a guard that
        only a race can reach is exactly the kind that rots unnoticed.
        """
        self.adapter.scan()
        self.imap.items[2] = message("Order received", "Thanks")
        self.adapter.scan()

        connection = sqlite3.connect(
            str(Path(self.directory.name) / "mail.sqlite3")
        )
        try:
            connection.execute(
                "UPDATE mail_observations SET reported_vanished = 1 WHERE uid = 2"
            )
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(
            next_arrival(self.state),
            "a pending row already known gone was promoted and offered",
        )


if __name__ == "__main__":
    unittest.main()
