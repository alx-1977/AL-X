"""Mail reaches the Core because the process is running, not because a browser is.

Watching the mailbox was already a property of the process. Thinking about what
it found was not: an observation reached the Core only through a generator a
live voice session drained, so whether AL/X could consider a message depended on
whether Friedl had a tab open. Mail found overnight waited for someone to
connect before it could even be judged irrelevant.

Mail is now an ordinary cognition occasion, like a matured self-request or a
finished task. The same producer protocol, the same ledger, the same runner, the
same lock, the same gateway entry point, the same authority. What changed is
that nothing in the path is a transport.

These tests are written around the three things that could go wrong with that:

- **It could still need a session.** Every test here constructs no VoiceSession
  at all, and the end-to-end one asserts that the transport is absent.
- **It could spend twice on one message.** Exactly-once is the shared ledger's
  job now rather than a slot in the mailbox, so the claim, the restart recovery
  and the identity scheme are all proved against it directly.
- **It could quietly gain authority.** An occasion is an invitation to think,
  never an instruction to act, and mail content is evidence whatever it says.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import BackgroundEvent  # noqa: E402
from alx.contracts.cognition import CognitionOrigin  # noqa: E402
from alx.continuity.ledger import SQLiteOpportunityLedger  # noqa: E402
from alx.continuity.mail_source import (  # noqa: E402
    OCCASION_PREFIX,
    MailCognitionSource,
    mail_conversation_id,
)
from alx.continuity.occasions import CombinedOccasionSource  # noqa: E402

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
CONVERSATION = mail_conversation_id("friedl")


def arrival(uid: str = "2") -> BackgroundEvent:
    return BackgroundEvent(
        f"mail:777:{uid}",
        "mail.message_arrived",
        NOW,
        {"mailbox_id": "INBOX", "uid_validity": "777", "uid": uid},
        {"body": f"body {uid}"},
    )


def vanished(uid: str = "1") -> BackgroundEvent:
    return BackgroundEvent(
        f"mail:777:{uid}:vanished",
        "mail.message_vanished",
        NOW,
        {"mailbox_id": "INBOX", "uid_validity": "777", "uid": uid},
    )


class FakeMailSource:
    """The durable observations, without an IMAP server behind them."""

    def __init__(self, arrivals=(), disappearances=()) -> None:
        self.arrivals = list(arrivals)
        self.disappearances = list(disappearances)
        self.delivered: list[str] = []

    def unclaimed_arrivals(self):
        return tuple(self.arrivals)

    def pending_vanished(self):
        return tuple(self.disappearances)

    def record_delivery(self, event_id: str) -> bool:
        self.delivered.append(event_id)
        return True


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = SQLiteOpportunityLedger(
            Path(self.directory.name) / "opportunities.sqlite3"
        )
        self.addCleanup(self.ledger.close)

    def source(self, mail, enabled: bool = True) -> MailCognitionSource:
        return MailCognitionSource(mail, self.ledger, CONVERSATION, enabled=enabled)


class MailBecomesAnOrdinaryOccasion(Fixture):
    """2, 3. What the producer makes, and what it never makes."""

    def test_an_arrival_becomes_an_external_event_occasion(self) -> None:
        source = self.source(FakeMailSource([arrival()]))

        occasions = source.due_opportunities()

        self.assertEqual(len(occasions), 1)
        self.assertIs(occasions[0].origin, CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(occasions[0].conversation_id, CONVERSATION)

    def test_a_disappearance_becomes_one_too(self) -> None:
        source = self.source(FakeMailSource(disappearances=[vanished()]))

        occasions = source.due_opportunities()

        self.assertEqual(len(occasions), 1)
        self.assertIs(occasions[0].origin, CognitionOrigin.EXTERNAL_EVENT)

    def test_no_occasion_is_ever_a_person_turn(self) -> None:
        """Mail must never be reasoned about as though Friedl had spoken."""
        source = self.source(
            FakeMailSource([arrival("2"), arrival("3")], [vanished()])
        )

        for occasion in source.due_opportunities():
            self.assertIsNot(occasion.origin, CognitionOrigin.PERSON_TURN)
            self.assertTrue(occasion.origin.is_autonomous)

    def test_the_master_switch_yields_nothing_and_touches_nothing(self) -> None:
        mail = FakeMailSource([arrival()])

        source = self.source(mail, enabled=False)

        self.assertEqual(source.due_opportunities(), ())
        self.assertEqual(mail.delivered, [])

    def test_the_occasion_carries_no_opinion_about_the_message(self) -> None:
        """No subject, sender, body or importance reaches the occasion."""
        source = self.source(FakeMailSource([arrival()]))

        occasion = source.due_opportunities()[0]

        rendered = repr(occasion)
        for leaked in ("body 2", "subject", "sender", "INBOX"):
            self.assertNotIn(leaked, rendered, leaked)
        self.assertIsNone(occasion.note)


class TheSharedLedgerProvidesExactlyOnce(Fixture):
    """4, 5. One message, one claim, one paid turn."""

    def test_a_claim_succeeds_once_and_then_refuses(self) -> None:
        source = self.source(FakeMailSource([arrival()]))
        occasion = source.due_opportunities()[0]

        self.assertTrue(source.claim(occasion))
        self.assertFalse(source.claim(occasion))

    def test_a_claimed_occasion_is_not_offered_again(self) -> None:
        source = self.source(FakeMailSource([arrival()]))
        source.claim(source.due_opportunities()[0])

        self.assertEqual(source.due_opportunities(), ())

    def test_a_released_occasion_can_arise_again(self) -> None:
        """A turn that never happened is not a thought she had."""
        source = self.source(FakeMailSource([arrival()]))
        occasion = source.due_opportunities()[0]
        source.claim(occasion)

        source.release(occasion)

        self.assertEqual(len(source.due_opportunities()), 1)

    def test_identities_are_stable_across_instances(self) -> None:
        """The same message yields the same occasion identity every time.

        A restart builds a new producer over the same durable observation, and
        the identity has to survive that or the ledger cannot recognise what it
        already claimed.
        """
        first = self.source(FakeMailSource([arrival()])).due_opportunities()[0]
        second = self.source(FakeMailSource([arrival()])).due_opportunities()[0]

        self.assertEqual(first.opportunity_id, second.opportunity_id)
        self.assertEqual(
            first.opportunity_id,
            MailCognitionSource.opportunity_id_for("mail:777:2"),
        )

    def test_an_arrival_and_its_disappearance_are_different_occasions(self) -> None:
        """Settling one says nothing about the other."""
        source = self.source(FakeMailSource([arrival("2")], [vanished("2")]))

        identities = {item.opportunity_id for item in source.due_opportunities()}

        self.assertEqual(len(identities), 2)
        self.assertIn(
            MailCognitionSource.opportunity_id_for("mail:777:2"), identities
        )
        self.assertIn(
            MailCognitionSource.opportunity_id_for("mail:777:2:vanished"),
            identities,
        )

    def test_the_occasion_identity_cannot_collide_with_the_observation(self) -> None:
        """The gateway merges both into one set keyed by event id.

        An occasion carrying the observation's own identity replaced it there,
        and the message body never reached the Core. They are two facts and
        they need two names.
        """
        source = self.source(FakeMailSource([arrival()]))
        occasion = source.due_opportunities()[0]

        self.assertNotEqual(occasion.opportunity_id, "mail:777:2")
        self.assertTrue(occasion.opportunity_id.startswith(OCCASION_PREFIX))

    def test_settling_reports_the_observation_not_the_occasion(self) -> None:
        mail = FakeMailSource([arrival()])
        source = self.source(mail)
        occasion = source.due_opportunities()[0]

        source.mark_honoured(occasion)

        self.assertEqual(mail.delivered, ["mail:777:2"])


class RestartRecoveryMatchesItsSiblings(Fixture):
    """6. The completed-work rule, for the completed-work reason."""

    class Spend:
        def __init__(self, dispatched=()) -> None:
            self._dispatched = set(dispatched)

        def dispatch_started(self, opportunity_id: str) -> bool:
            return opportunity_id in self._dispatched

    def test_an_undispatched_claim_is_reclaimed(self) -> None:
        """No provider was reached, so the occasion may arise again."""
        source = self.source(FakeMailSource([arrival()]))
        occasion = source.due_opportunities()[0]
        source.claim(occasion)

        reclaimed = source.recover(self.Spend())

        self.assertEqual(reclaimed, (occasion.opportunity_id,))
        self.assertEqual(len(source.due_opportunities()), 1)

    def test_a_dispatched_claim_is_retained_never_replayed(self) -> None:
        """It may already have been billed and answered."""
        source = self.source(FakeMailSource([arrival()]))
        occasion = source.due_opportunities()[0]
        source.claim(occasion)

        reclaimed = source.recover(self.Spend({occasion.opportunity_id}))

        self.assertEqual(reclaimed, ())
        self.assertEqual(source.due_opportunities(), ())

    def test_only_this_producers_rows_are_touched(self) -> None:
        """The ledger is shared; another producer keeps its own idempotence."""
        from alx.contracts import CognitionOpportunity

        foreign = CognitionOpportunity(
            opportunity_id="task:review-1",
            origin=CognitionOrigin.WORK_COMPLETED,
            arose_at=NOW,
            conversation_id="conversation-1",
            references=("external_task:review-1",),
        )
        self.ledger.record_created(foreign)
        source = self.source(FakeMailSource([arrival()]))
        source.claim(source.due_opportunities()[0])

        source.recover(self.Spend())

        self.assertTrue(self.ledger.exists("task:review-1"))

    def test_recovery_is_idempotent(self) -> None:
        source = self.source(FakeMailSource([arrival()]))
        source.claim(source.due_opportunities()[0])

        source.recover(self.Spend())

        self.assertEqual(source.recover(self.Spend()), ())


class MailJoinsTheOneProducer(Fixture):
    """13. One combined source, one tick, one runner. No second route."""

    def test_the_combined_source_delegates_mail_back_to_its_owner(self) -> None:
        mail = FakeMailSource([arrival()])
        source = self.source(mail)
        combined = CombinedOccasionSource(source)

        occasion = combined.due_opportunities()[0]

        self.assertTrue(source.owns(occasion))
        self.assertTrue(combined.claim(occasion))
        combined.mark_honoured(occasion)
        self.assertEqual(mail.delivered, ["mail:777:2"])

    def test_it_does_not_claim_another_producers_occasion(self) -> None:
        from alx.contracts import CognitionOpportunity

        source = self.source(FakeMailSource())
        foreign = CognitionOpportunity(
            opportunity_id="self:request-1",
            origin=CognitionOrigin.SELF_REQUESTED,
            arose_at=NOW,
            conversation_id="conversation-1",
            references=("future_cognition:request-1",),
        )

        self.assertFalse(source.owns(foreign))

    def test_no_second_route_feeds_mail_content_to_the_core(self) -> None:
        """Law 0: the removed transport path must not have come back."""
        root = Path(__file__).resolve().parents[1] / "src" / "alx"
        session = (root / "interfaces" / "live_voice.py").read_text()
        gateway = (root / "conversation" / "gateway.py").read_text()
        adapter = (root / "providers" / "icloud_mail.py").read_text()

        for removed in ("receive_background_event", "event_source", "_event_source"):
            self.assertNotIn(removed, session, removed)
        self.assertNotIn("def receive_background_event", gateway)
        self.assertNotIn("async def events", adapter)


class NoSessionIsRequired(Fixture):
    """1, 9, 10, 11, 14. The whole point, end to end."""

    def _runtime(self, mail, decision=None, transport=None):
        """The real composition, minus the transport entirely."""
        from alx.bootstrap.autonomous import AutonomousCognitionRunner
        from alx.contracts import AgentDecision
        from alx.conversation import ConversationGateway, SQLiteConversationStore
        from alx.core import CoreAgent
        from alx.goals import SQLiteGoalStore

        root = Path(self.directory.name)
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        self.addCleanup(conversations.close)
        goals = SQLiteGoalStore(root / "goals.sqlite3")
        self.addCleanup(goals.close)

        self.person = Recording(AgentDecision(response="person"))
        self.autonomous = Recording(
            decision or AgentDecision(response="A supplier sent a quote.")
        )
        from alx.bootstrap.reasoning import OriginSelectedReasoner

        core = CoreAgent(
            goals,
            OriginSelectedReasoner(self.person, self.autonomous),
            lambda call, state: None,
            (),
        )
        gateway = ConversationGateway(
            core,
            conversations,
            identifier_factory=lambda: "response-1",
            clock=lambda: NOW,
            contextual_events=lambda: tuple(mail.unclaimed_arrivals()),
        )
        source = self.source(mail)
        runner = AutonomousCognitionRunner(
            source, self.ledger, gateway, 4, 3650,
            response_transport=transport, clock=lambda: NOW,
        )
        return source, runner

    def test_mail_reaches_the_core_with_no_voice_session_constructed(self) -> None:
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)

        ran = runner.run_one(source.due_opportunities()[0])

        self.assertTrue(ran)
        self.assertEqual(len(self.autonomous.contexts), 1)
        self.assertEqual(self.person.contexts, [], "no person turn happened")

    def test_the_turn_is_an_external_event_turn(self) -> None:
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)

        runner.run_one(source.due_opportunities()[0])

        self.assertIs(
            self.autonomous.contexts[0].origin, CognitionOrigin.EXTERNAL_EVENT
        )

    def test_mail_never_reaches_the_conversational_reasoner(self) -> None:
        mail = FakeMailSource([arrival()], [vanished()])
        source, runner = self._runtime(mail)

        for occasion in source.due_opportunities():
            runner.run_one(occasion)

        self.assertEqual(self.person.contexts, [])
        self.assertEqual(len(self.autonomous.contexts), 2)

    def test_the_message_body_reaches_the_core_as_evidence(self) -> None:
        """14. Content is data on the evidence channel, never instruction."""
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)

        runner.run_one(source.due_opportunities()[0])

        events = self.autonomous.contexts[0].events
        observed = [item for item in events if item.kind == "mail.message_arrived"]
        self.assertEqual(len(observed), 1, "the observation reached the turn")
        self.assertEqual(observed[0].transient_data["body"], "body 2")
        # It arrived as an event to reason about, not as a turn she must obey.
        self.assertNotIn(
            "mail.message_arrived",
            [turn.origin.value for turn in self.autonomous.contexts[0].turns],
        )

    def test_an_undeliverable_response_is_retained_not_lost(self) -> None:
        """10. Nobody is connected, so there is nowhere for it to go."""
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)

        occasion = source.due_opportunities()[0]
        runner.run_one(occasion)

        undelivered = [
            row["opportunity_id"] for row in self.ledger.undelivered()
        ]
        self.assertIn(occasion.opportunity_id, undelivered)

    def test_a_later_session_is_offered_what_she_missed(self) -> None:
        """11. Through the existing mechanism, not a second notifier."""
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)
        runner.run_one(source.due_opportunities()[0])

        # What the composition root hands the Core as context on a later turn.
        offered = self.ledger.undelivered()

        self.assertEqual(len(offered), 1)
        self.assertNotIn(
            "A supplier sent a quote.",
            repr(offered),
            "the wording is not stored, so nothing can replay it",
        )

    def test_a_delivered_response_is_not_marked_undelivered(self) -> None:
        from alx.contracts import ResponseDelivery

        class Connected:
            def __init__(self) -> None:
                self.delivered = []

            def deliver(self, conversation_id, response):
                self.delivered.append((conversation_id, response))
                return ResponseDelivery.DELIVERED

        transport = Connected()
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail, transport=transport)

        runner.run_one(source.due_opportunities()[0])

        self.assertEqual(len(transport.delivered), 1)
        self.assertEqual(transport.delivered[0][0], CONVERSATION)
        self.assertEqual(self.ledger.undelivered(), ())

    def test_silence_is_an_ordinary_outcome(self) -> None:
        """Nothing is spoken and nothing is retained when she says nothing."""
        from alx.contracts import AgentDecision

        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(
            mail, decision=AgentDecision(finish_silently=True)
        )

        runner.run_one(source.due_opportunities()[0])

        self.assertEqual(self.ledger.undelivered(), ())

    def test_the_observation_is_settled_once_the_turn_has_run(self) -> None:
        mail = FakeMailSource([arrival()])
        source, runner = self._runtime(mail)

        runner.run_one(source.due_opportunities()[0])

        self.assertEqual(mail.delivered, ["mail:777:2"])


class Recording:
    """A reasoner that answers once and remembers what it was shown."""

    def __init__(self, decision) -> None:
        self._decision = decision
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self._decision


class ExistingBoundsApplyToMail(Fixture):
    """7, 8, 12. Mail buys no exemption from anything."""

    def _runner(self, mail, **changes):
        from alx.bootstrap.autonomous import AutonomousCognitionRunner

        values = {
            "source": self.source(mail),
            "ledger": self.ledger,
            "gateway": self.Gateway(),
            "step_budget": 4,
            "retention_days": 3650,
        }
        values.update(changes)
        source = values.pop("source")
        return source, AutonomousCognitionRunner(
            source,
            values["ledger"],
            values["gateway"],
            values["step_budget"],
            values["retention_days"],
            clock=lambda: NOW,
            commissioning_limit=values.get("commissioning_limit"),
        )

    class Gateway:
        def __init__(self) -> None:
            self.calls = []

        def receive_cognition_opportunity(
            self, conversation_id, opportunity, step_budget, retention_until
        ):
            from alx.core import CoreState
            from alx.core.loop import CoreOutcome

            self.calls.append((conversation_id, opportunity, step_budget))
            return CoreOutcome(
                state=CoreState.FINISHED_SILENTLY, snapshot=None, response=None
            )

    def test_the_configured_step_budget_is_applied(self) -> None:
        gateway = self.Gateway()
        source, runner = self._runner(
            FakeMailSource([arrival()]), gateway=gateway, step_budget=3
        )

        runner.run_one(source.due_opportunities()[0])

        self.assertEqual(gateway.calls[0][2], 3)

    def test_the_commissioning_latch_refuses_beyond_its_limit(self) -> None:
        gateway = self.Gateway()
        source, runner = self._runner(
            FakeMailSource([arrival("2"), arrival("3")]),
            gateway=gateway,
            commissioning_limit=1,
        )

        occasions = source.due_opportunities()
        runner.run_one(occasions[0])
        runner.run_one(occasions[1])

        self.assertEqual(len(gateway.calls), 1, "the latch closed after one")

    def test_the_turn_runs_on_the_bound_conversation(self) -> None:
        """Mail accumulates in one durable thread, not a browser's."""
        gateway = self.Gateway()
        source, runner = self._runner(FakeMailSource([arrival()]), gateway=gateway)

        runner.run_one(source.due_opportunities()[0])

        self.assertEqual(gateway.calls[0][0], CONVERSATION)

    def test_a_daily_ceiling_refusal_stops_the_turn(self) -> None:
        """The ledger fails closed; mail is not an exception to it."""

        class Exhausted(self.Gateway):
            def receive_cognition_opportunity(self, *arguments):
                from alx.observability import BudgetExceeded

                raise BudgetExceeded("daily autonomous ceiling reached")

        gateway = Exhausted()
        source, runner = self._runner(FakeMailSource([arrival()]), gateway=gateway)
        occasion = source.due_opportunities()[0]

        runner.run_one(occasion)

        # The turn did not happen, so the occasion is given back rather than
        # kept: she is not left waiting on a cognition that cannot arrive.
        self.assertEqual(len(source.due_opportunities()), 1)


class TheConversationIsStableAndScoped(unittest.TestCase):
    """Where mail thinking accumulates."""

    def test_the_thread_is_derived_not_generated(self) -> None:
        self.assertEqual(
            mail_conversation_id("friedl"), mail_conversation_id("friedl")
        )

    def test_it_is_scoped_by_person(self) -> None:
        self.assertNotEqual(
            mail_conversation_id("friedl"), mail_conversation_id("someone-else")
        )

    def test_a_blank_person_is_refused(self) -> None:
        for value in ("", "   "):
            with self.assertRaises(ValueError):
                mail_conversation_id(value)

    def test_a_blank_conversation_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            MailCognitionSource(FakeMailSource(), None, "   ")


if __name__ == "__main__":
    unittest.main()
