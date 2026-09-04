"""The observation store as one concurrent state machine.

Greptile found two P1 defects in the first version of this branch, both from
treating the store as if one writer owned it.

`pending` was settled silently on the premise that it had never been announced.
That premise held when the branch began and stopped holding two commits later,
when `mail.message_waiting` began showing pending observations to the Core. A
message AL/X had been shown could then disappear with no way for her to account
for it.

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
    """P1 #1 — what the Core has been shown is hers to account for."""

    def test_a_pending_observation_never_shown_vanishes_silently(self) -> None:
        self.discover(1, 2)
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        self.assertEqual(self.rows()[2][0], "done")
        self.assertEqual(self.state.pending_vanished(), ())

    def test_a_pending_observation_shown_as_waiting_reaches_her(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()          # uid 2 is shown as waiting
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 1)
        self.assertEqual(
            [event.data["uid"] for event in self.state.pending_vanished()], ["2"],
            "she was shown it, so its disappearance is hers to account for",
        )
        self.assertEqual(
            self.rows()[2][0], "pending",
            "and it keeps its state until she releases it",
        )

    def test_an_exposed_disappearance_reaches_her_exactly_once(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.reconcile("INBOX", VALIDITY, (1,))
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 0)
        event = self.state.pending_vanished()[0]
        self.state.record_vanished_delivery(event.event_id)
        self.assertEqual(self.state.pending_vanished(), ())

    def test_exposure_survives_a_restart(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.close()
        restarted = SQLiteMailObservationState(self.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile("INBOX", VALIDITY, (1,)), 1)
        self.assertEqual(
            [event.data["uid"] for event in restarted.pending_vanished()], ["2"],
        )

    def test_exposure_never_reverts(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.state.contextual_events()
        self.assertEqual(self.rows()[2][1], 1)

    def test_being_shown_does_not_announce_or_advance_anything(self) -> None:
        self.discover(1, 2)
        self.state.contextual_events()
        self.assertEqual(self.rows()[2][0], "pending")
        self.assertEqual(self.state.current().data["uid"], "1")

    def test_exposure_is_recorded_before_the_turn_runs(self) -> None:
        """A deliberate direction to err in, not an oversight.

        The mark is written as context is built. A turn that then fails leaves
        a row marked exposed that AL/X never evaluated, so a later
        disappearance gives her one fact she did not strictly need. The
        alternative -- marking only after a successful turn -- loses the mark
        when a turn that *did* show her the mail fails afterwards, and the
        disappearance is then settled silently. An unnecessary fact costs one
        reasoning call she can dismiss; a missing one leaves her unable to
        account for something she may have raised.
        """
        self.discover(1, 2)
        self.state.contextual_events()          # the turn has not run yet
        self.assertEqual(self.rows()[2][1], 1)
        # The turn now fails and uid 2 later leaves the mailbox.
        self.assertEqual(self.state.reconcile("INBOX", VALIDITY, (1,)), 1)
        self.assertEqual(
            [event.data["uid"] for event in self.state.pending_vanished()], ["2"],
            "she is given the fact rather than losing it",
        )

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
        event = self.state.current()
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
        event = self.state.current()
        self.state.record_delivery(event.event_id)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        marked = self.state._mark_vanished("INBOX", VALIDITY, 1, "presented")
        self.assertFalse(marked)
        self.assertEqual(self.state.pending_vanished(), ())

    def test_current_does_not_promote_a_row_settled_meanwhile(self) -> None:
        self.discover(1)
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertIsNone(self.state.current())
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
                    item = self.state.current()
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
        holding = self.state.current()
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
        holding = self.state.current()
        self.state.record_delivery(holding.event_id)
        waiting = [
            int(event.data["uid"])
            for event in self.state.contextual_events()
            if event.kind == "mail.message_waiting"
        ]
        self.state.acknowledge(MailReference("INBOX", VALIDITY, "1"))
        self.assertEqual(int(self.state.current().data["uid"]), waiting[0])


class VanishedIdentifierTest(Harness):
    """P2 #5 — malformed identifiers fail with the documented domain error."""

    def test_a_valid_vanished_identifier_is_accepted(self) -> None:
        self.discover(1)
        event = self.state.current()
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


if __name__ == "__main__":
    unittest.main()
