"""The observation store as one concurrent state machine.

Greptile found two P1 defects in the first version of this branch, both from
treating the store as if one writer owned it.

`pending` is settled silently when it is absent or Seen, including after it
has been shown as waiting context. Waiting exposure is not a vanished debt.
A current or presented row that has left the mailbox is reported once, and
that report is what a claimed occasion owes.

And reconciliation read a row's state in one transaction and wrote it in
another with no state predicate, so a session could promote and announce a row
in the window between, and the write would overwrite `presented` with `done` --
losing the announcement entirely.

The rule these tests hold: every transition names the state it expects to
replace, and a transition that matches nothing has been overtaken and is
re-read rather than forced.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.contracts import MailAccessError, MailReference  # noqa: E402
from alx.providers.icloud_mail import SQLiteMailObservationState  # noqa: E402

def next_arrival(state):
    """The oldest arrival still awaiting delivery, or None.

    What the removed single-slot reader returned, over the reader that
    replaced it. Exactly-once is the opportunity ledger's job now, so the
    store reports the whole queue and "the next one" is the caller's question.
    """
    awaiting = state.unclaimed_arrivals()
    return awaiting[0] if awaiting else None


VALIDITY = "777"
SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "alx" / "providers" / "icloud_mail.py"
)


def observed(uid: int) -> tuple[int, dict[str, str]]:
    return (uid, {
        "mailbox_id": "INBOX", "uid_validity": VALIDITY, "uid": str(uid),
        "observed_at": "2026-09-04T06:00:00+00:00",
        "subject": f"Message {uid}",
        "sender": f"someone{uid}@example.test",
    })


class Harness(unittest.TestCase):
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

    def rows(self) -> dict[int, tuple[str, int, int]]:
        return {
            int(uid): (state, exposed, vanished)
            for uid, state, exposed, vanished in self.state._connection.execute(
                "SELECT uid, state, context_exposed, reported_vanished "
                "FROM mail_observations"
            )
        }


class ExposedPendingAuthorityTest(Harness):
    """Waiting exposure is not a vanished debt.

    A pending row the Core has been shown is still pending. Its absence, or
    a Seen flag, settles it to done and reports nothing. A vanished report
    is owed for a current or presented row that has left the mailbox.
    """

    def waiting(self) -> list[str]:
        return [
            event.data["uid"]
            for event in self.state.contextual_events()
            if event.kind == "mail.message_waiting"
        ]

    def test_a_pending_observation_never_shown_vanishes_silently(self) -> None:
        self.discover(1, 2)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_pending_observation_shown_as_waiting_is_released(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()          # uid 2 is shown as waiting
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())
        self.assertNotIn("2", self.waiting())

    def test_a_waiting_absence_is_not_reported_again(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.reconcile("INBOX", VALIDITY, (1,))
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_waiting_absence_stays_settled_across_restart(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.close()
        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(restarted.pending_vanished(), ())
        row = restarted._connection.execute(
            "SELECT state FROM mail_observations WHERE uid = 2"
        ).fetchone()
        self.assertEqual(row[0], "done")

    def test_exposure_never_reverts(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.contextual_events()
        self.assertEqual(self.rows()[2][1], 1)

    def test_being_shown_does_not_announce_or_advance_anything(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.assertEqual(self.rows()[2][0], "pending")
        self.assertEqual(next_arrival(self.state).data["uid"], "1")

    def test_exposure_is_recorded_before_the_turn_runs(self) -> None:
        """Seeing the queue marks the row, and the row stays pending.

        The mark is written as context is built. A pending absence afterwards
        is still a silent release: waiting exposure is not a vanished debt.
        """
        self.discover(1, 2)
        self.state.contextual_events()          # the turn has not run yet
        self.assertEqual(self.rows()[2], ("pending", 1, 0))
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())
        self.assertNotIn("2", self.waiting())

    def test_a_seen_pending_row_is_released_beside_an_unseen_sibling(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.assertEqual(
            self.state.reconcile("INBOX", VALIDITY, ((1, False), (2, True))),
            0,
        )
        self.assertEqual(self.rows()[1][0], "pending")
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())
        self.assertEqual(self.waiting(), ["1"])

    def test_a_failed_listing_writes_nothing(self) -> None:
        self.discover(1, 2)
        before = self.rows()
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, None), 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_current_absence_is_reported_once_across_repeats_and_restart(self) -> None:
        self.discover(1)
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.assertEqual(self.rows()[1][0], "current")
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 1)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 0)
        state, _exposed, vanished = self.rows()[1]
        self.assertEqual((state, vanished), ("current", 1))
        self.assertEqual(len(self.state.pending_vanished()), 1)
        self.state.close()
        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile("INBOX", VALIDITY, ()), 0)
        self.assertEqual(
            [event.data["uid"] for event in restarted.pending_vanished()], ["1"],
        )

    def test_a_seen_current_row_that_is_still_present_is_left_alone(self) -> None:
        self.discover(1)
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.assertEqual(
            self.state.reconcile("INBOX", VALIDITY, ((1, True),)), 0,
        )
        self.assertEqual(self.rows()[1], ("current", 1, 0))
        self.assertEqual(self.state.pending_vanished(), ())

    def test_mark_claimed_promotes_pending_to_current(self) -> None:
        self.discover(1)
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.assertEqual(self.rows()[1], ("current", 1, 0))
        # Already current: the update matches nothing, the read-back is live.
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.assertEqual(self.rows()[1][0], "current")

    def test_mark_claimed_does_not_rewrite_a_presented_row(self) -> None:
        self.discover(1)
        self.state.contextual_events()
        event = next_arrival(self.state)
        self.assertTrue(self.state.record_delivery(event.event_id))
        self.assertEqual(self.rows()[1][0], "presented")
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.assertEqual(self.rows()[1][0], "presented")

    def test_mark_claimed_is_false_when_nothing_live_remains(self) -> None:
        self.discover(1)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertFalse(self.state.mark_claimed(f"mail:{VALIDITY}:1"))

    def test_a_new_uid_validity_deletes_no_observation_rows(self) -> None:
        self.discover(1, 2)
        self.assertEqual(self.state.new_identifiers("INBOX", "888", (4,)), ())
        self.assertEqual(set(self.rows()), {1, 2})
        self.assertEqual(self.rows()[1][0], "done")
        self.assertEqual(self.state.unclaimed_arrivals(), ())
        self.assertEqual(self.state.contextual_events(), ())
        cursor = self.state._connection.execute(
            "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
            ("INBOX",),
        ).fetchone()
        self.assertEqual(cursor, ("888", 4))

    def test_a_new_generation_reports_old_claimed_mail_once(self) -> None:
        self.discover(1, 2)
        self.assertTrue(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.state.new_identifiers("INBOX", "888", (1,))
        self.assertEqual(self.rows()[1], ("current", 1, 1))
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.unclaimed_arrivals(), ())
        self.assertEqual(
            [event.kind for event in self.state.contextual_events()],
            ["mail.message_vanished"],
        )
        self.assertEqual(
            [event.data["uid"] for event in self.state.pending_vanished()],
            ["1"],
        )
        self.state.new_identifiers("INBOX", "888", (1,))
        self.assertEqual(len(self.state.pending_vanished()), 1)
        self.assertFalse(self.state.mark_claimed(f"mail:{VALIDITY}:1"))
        self.state.record_vanished_delivery(f"mail:{VALIDITY}:1:vanished")
        self.assertEqual(self.state.contextual_events(), ())

    def test_a_different_cursor_generation_reconciles_only_this_one(self) -> None:
        self.discover(1)
        self.state._connection.execute(
            "INSERT INTO mail_observations "
            "(mailbox_id, uid_validity, uid, event_json, state) "
            "VALUES ('INBOX', '999', 4, '{}', 'current')"
        )
        self.state._connection.commit()
        self.state.new_identifiers("INBOX", "999", ())
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, ()), 0)
        self.assertEqual(self.rows()[1][0], "done")
        self.assertEqual(self.rows()[4][0], "current")
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_pending_row_already_marked_vanished_is_not_waiting(self) -> None:
        self.discover(1)
        self.state._connection.execute(
            "UPDATE mail_observations SET reported_vanished = 1 WHERE uid = 1"
        )
        self.state._connection.commit()
        self.assertEqual(self.waiting(), [])
        self.assertEqual(
            [event.data["uid"] for event in self.state.pending_vanished()],
            ["1"],
        )

    def test_one_reconcile_and_no_observation_delete(self) -> None:
        text = SOURCE.read_text()
        self.assertEqual(text.count("\n    def reconcile("), 1)
        self.assertNotIn("DELETE FROM mail_observations", text)

    def test_the_branch_reads_no_subject_or_sender(self) -> None:
        """Law 1: the choice is made from state, never from content."""
        text = SOURCE.read_text()
        body = text[text.index("    def reconcile("):]
        body = body[: body.index("\n    def _settle_silently(")]
        # Executable lines only: prose about not reading content is not code.
        code = "\n".join(
            line for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        code = code[code.index('"""', code.index('"""') + 3) + 3:]
        for token in ("subject", "sender", "message_id", "event_json"):
            with self.subTest(token=token):
                self.assertNotIn(token, code)


class ReconciliationRaceTest(Harness):
    """P1 #2 — a transition may never overwrite a state it did not observe."""

    def test_reconciliation_cannot_overwrite_a_row_promoted_meanwhile(self) -> None:
        """The exact interleaving Greptile described, made deterministic."""
        self.discover(1)
        # Reconciliation observed uid 1 as `pending` and unexposed. Before its
        # write lands, a session promotes and announces it.
        observed_state = "pending"
        event = next_arrival(self.state)
        self.state.record_delivery(event.event_id)
        self.assertEqual(self.rows()[1][0], "presented")

        settled = self.state._settle_silently("INBOX", VALIDITY, 1)

        self.assertFalse(settled, "the stale transition must not apply")
        self.assertEqual(
            self.rows()[1][0], "presented",
            "an announced observation must not be overwritten as done",
        )
        self.assertEqual(observed_state, "pending")

    def test_a_stale_vanished_mark_does_not_resurrect_a_released_row(self) -> None:
        self.discover(1)
        event = next_arrival(self.state)
        self.state.record_delivery(event.event_id)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        marked = self.state._mark_vanished("INBOX", VALIDITY, 1, "presented")
        self.assertFalse(marked)
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_row_settled_meanwhile_is_not_offered(self) -> None:
        self.discover(1)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertIsNone(next_arrival(self.state))
        self.assertEqual(self.rows()[1][0], "done")

    def test_acknowledging_twice_reports_the_second_as_unavailable(self) -> None:
        self.discover(1)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        with self.assertRaises(MailAccessError):
            self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))

    def test_concurrent_reconciliation_and_delivery_stay_consistent(self) -> None:
        """Two threads, the real lock, over many rows: no row is lost."""
        uids = tuple(range(1, 41))
        self.discover(*uids)
        errors: list[BaseException] = []

        def reconcile() -> None:
            try:
                for _ in range(30):
                    self.state.reconcile("INBOX", VALIDITY, ())
            except BaseException as error:      # noqa: BLE001
                errors.append(error)

        def deliver() -> None:
            try:
                for _ in range(30):
                    item = next_arrival(self.state)
                    if item is not None:
                        self.state.record_delivery(item.event_id)
                    self.state.contextual_events()
            except BaseException as error:      # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=reconcile),
                   threading.Thread(target=deliver)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        rows = self.rows()
        self.assertEqual(len(rows), len(uids), "no row was lost")
        for uid, (state, _exposed, vanished) in rows.items():
            with self.subTest(uid=uid):
                self.assertIn(state, ("pending", "current", "presented", "done"))
                self.assertIn(vanished, (0, 1, 2))
                if state == "done":
                    self.assertNotEqual(
                        vanished, 1,
                        "a settled row must not hold an undelivered fact",
                    )

    def test_every_state_transition_names_the_state_it_replaces(self) -> None:
        """Mutation guard: an unpredicated state write is the P1 #2 defect."""
        text = SOURCE.read_text()
        writes = [
            line.strip()
            for line in text.splitlines()
            if "UPDATE mail_observations SET state" in line
        ]
        self.assertTrue(writes)
        # Each such write is followed by a WHERE that constrains the prior state.
        for write in writes:
            index = text.index(write)
            clause = text[index: index + 400]
            with self.subTest(write=write[:60]):
                self.assertIn("state", clause.split("WHERE", 1)[1][:200])


class WaitingOrderTest(Harness):
    """P2 #3 — waiting context follows delivery order."""

    def test_the_oldest_pending_are_shown_not_the_newest(self) -> None:
        uids = tuple(range(1, 21))
        self.discover(*uids)
        holding = next_arrival(self.state)
        self.state.record_delivery(holding.event_id)
        waiting = [
            int(event.data["uid"])
            for event in self.state.contextual_events()
            if event.kind == "mail.message_waiting"
        ]
        self.assertEqual(len(waiting), self.state.WAITING_EVENT_LIMIT)
        self.assertEqual(waiting, sorted(waiting), "delivery order, oldest first")
        self.assertEqual(waiting[0], 2, "the very next item to be delivered")
        self.assertNotIn(20, waiting, "newer mail must not hide older mail")

    def test_what_she_is_shown_is_what_she_will_be_given_next(self) -> None:
        uids = tuple(range(1, 21))
        self.discover(*uids)
        holding = next_arrival(self.state)
        self.state.record_delivery(holding.event_id)
        waiting = [
            int(event.data["uid"])
            for event in self.state.contextual_events()
            if event.kind == "mail.message_waiting"
        ]
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertEqual(int(next_arrival(self.state).data["uid"]), waiting[0])


class VanishedIdentifierTest(Harness):
    """P2 #5 — malformed identifiers fail with the documented domain error."""

    def test_a_valid_vanished_identifier_is_accepted(self) -> None:
        self.discover(1)
        event = next_arrival(self.state)
        self.state.record_delivery(event.event_id)
        self.state.reconcile("INBOX", VALIDITY, ())
        vanished = self.state.pending_vanished()[0]
        self.assertTrue(self.state.record_vanished_delivery(vanished.event_id))

    def test_a_non_numeric_uid_raises_the_domain_error(self) -> None:
        with self.assertRaises(MailAccessError) as caught:
            self.state.record_vanished_delivery(f"mail:{VALIDITY}:x:vanished")
        self.assertEqual(caught.exception.code, "observation_unavailable")

    def test_a_malformed_shape_raises_the_domain_error(self) -> None:
        for identifier in (
            "mail:777:vanished",
            "mail:777:1:gone",
            "post:777:1:vanished",
            "mail:777:1:vanished:extra",
            "",
        ):
            with self.subTest(identifier=identifier):
                with self.assertRaises(MailAccessError):
                    self.state.record_vanished_delivery(identifier)

    def test_no_raw_value_error_escapes_the_delivery_path(self) -> None:
        for identifier in (f"mail:{VALIDITY}:x:vanished", "mail:a:b:vanished"):
            with self.subTest(identifier=identifier):
                try:
                    self.state.record_delivery(identifier)
                except MailAccessError:
                    pass
                except ValueError as error:     # pragma: no cover
                    self.fail(f"raw ValueError escaped: {error}")


class PromptJudgementTest(unittest.TestCase):
    """P2 #4 — the prompt describes structure, never what mail matters."""

    def guidance(self) -> str:
        text = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "core" / "model_reasoner.py"
        ).read_text()
        start = text.index("Mail attention is deliberately one item at a time.")
        return text[start: text.index("Do not answer questions,")].lower()

    def test_no_standing_category_judgement_remains(self) -> None:
        for phrase in (
            "mostly receipts",
            "receipts, notifications",
            "usually right",
            "things addressed to nobody",
            "very likely dealt with it",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.guidance())

    def test_the_structural_facts_remain(self) -> None:
        guidance = self.guidance()
        for phrase in (
            "mail.message_waiting",
            "mail.message_vanished",
            "acknowledgement",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guidance)

    def test_judgement_is_returned_to_her(self) -> None:
        self.assertIn("your judgement", self.guidance())

    def test_no_category_prior_survives_in_the_mail_provider_either(self) -> None:
        """The prompt was cleaned; a comment beside the code had kept the prior.

        Removing a standing assumption from the guidance but leaving it in the
        source that produces the context is only half a fix: the next person to
        read the provider learns the rule anyway.
        """
        text = (
            Path(__file__).resolve().parents[1]
            / "src" / "alx" / "providers" / "icloud_mail.py"
        ).read_text().lower()
        for phrase in ("mostly receipts", "receipts and notifications",
                       "usually right", "addressed to nobody"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
