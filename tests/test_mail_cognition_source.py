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


def arrival(uid: str = "2", message_id: str = "", **threading) -> BackgroundEvent:
    data = {
        "mailbox_id": "INBOX",
        "uid_validity": "777",
        "uid": uid,
        "message_id": message_id or f"<m{uid}@example.test>",
    }
    data.update(threading)
    return BackgroundEvent(
        f"mail:777:{uid}",
        "mail.message_arrived",
        NOW,
        data,
        {"body": f"body {uid}"},
    )


def vanished(uid: str = "1", message_id: str = "") -> BackgroundEvent:
    return BackgroundEvent(
        f"mail:777:{uid}:vanished",
        "mail.message_vanished",
        NOW,
        {
            "mailbox_id": "INBOX",
            "uid_validity": "777",
            "uid": uid,
            "message_id": message_id or f"<m{uid}@example.test>",
        },
    )


# The thread the default arrival fixture belongs to, derived the way the
# producer derives it.
CONVERSATION = mail_conversation_id(arrival())


class FakeMailSource:
    """The durable observations, without an IMAP server behind them."""

    def __init__(self, arrivals=(), disappearances=()) -> None:
        self.arrivals = list(arrivals)
        self.disappearances = list(disappearances)
        self.delivered: list[str] = []
        self.claimed: list[str] = []

    def unclaimed_arrivals(self):
        return tuple(self.arrivals)

    def pending_vanished(self):
        return tuple(self.disappearances)

    def record_delivery(self, event_id: str) -> bool:
        self.delivered.append(event_id)
        return True

    def mark_claimed(self, event_id: str) -> bool:
        self.claimed.append(event_id)
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
        return MailCognitionSource(mail, self.ledger, enabled=enabled)


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


class EachThreadHasItsOwnDurableConversation(Fixture):
    """Unrelated correspondence must not share a history.

    The runtime keyed every message on `mail:<primary_person_id>`, so one
    mailbox was one conversation. Every correspondent, subject and unfinished
    goal accumulated there together: AL/X reasoning about a supplier's quote
    could see, select and continue a goal belonging to an unrelated thread.

    A thread is now named by RFC 5322 identifier headers -- the root of the
    References chain, else In-Reply-To, else the message's own Message-ID.
    Mechanical, restart-stable, and never inferred from what a subject line
    appears to mean.
    """

    def test_two_unrelated_threads_get_different_conversations(self) -> None:
        quote = arrival("2", "<quote@example.test>")
        invoice = arrival("3", "<invoice@example.test>")

        self.assertNotEqual(
            mail_conversation_id(quote), mail_conversation_id(invoice)
        )

    def test_a_reply_continues_the_thread_it_replies_to(self) -> None:
        original = arrival("2", "<quote@example.test>")
        reply = arrival(
            "3",
            "<reply@example.test>",
            in_reply_to="<quote@example.test>",
            references=["<quote@example.test>"],
        )

        self.assertEqual(
            mail_conversation_id(original), mail_conversation_id(reply)
        )

    def test_a_deep_thread_resolves_to_its_root(self) -> None:
        """Every message in one real thread, however long, is one conversation."""
        root = arrival("2", "<a@example.test>")
        second = arrival(
            "3", "<b@example.test>",
            in_reply_to="<a@example.test>",
            references=["<a@example.test>"],
        )
        third = arrival(
            "4", "<c@example.test>",
            in_reply_to="<b@example.test>",
            references=["<a@example.test>", "<b@example.test>"],
        )

        identities = {
            mail_conversation_id(item) for item in (root, second, third)
        }

        self.assertEqual(len(identities), 1)

    def test_a_reply_without_a_chain_still_finds_its_parent(self) -> None:
        """Some clients send In-Reply-To and no References."""
        original = arrival("2", "<quote@example.test>")
        reply = arrival("3", "<reply@example.test>", in_reply_to="<quote@example.test>")

        self.assertEqual(
            mail_conversation_id(original), mail_conversation_id(reply)
        )

    def test_identity_is_not_inferred_from_the_subject(self) -> None:
        """Two unrelated messages can share a subject; that means nothing."""
        first = arrival("2", "<one@example.test>", subject="Invoice")
        second = arrival("3", "<two@example.test>", subject="Invoice")

        self.assertNotEqual(
            mail_conversation_id(first), mail_conversation_id(second)
        )

    def test_a_message_with_no_identifiers_gets_its_own_thread(self) -> None:
        """The safe direction: a thread too narrow, never one too wide."""
        first = BackgroundEvent(
            "mail:777:9", "mail.message_arrived", NOW,
            {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "9"},
        )
        second = BackgroundEvent(
            "mail:777:10", "mail.message_arrived", NOW,
            {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "10"},
        )

        self.assertNotEqual(
            mail_conversation_id(first), mail_conversation_id(second)
        )

    def test_a_disappearance_settles_in_its_own_thread(self) -> None:
        """The vanished fact belongs where the arrival did."""
        original = arrival("2", "<quote@example.test>")
        gone = vanished("2", "<quote@example.test>")

        self.assertEqual(
            mail_conversation_id(original), mail_conversation_id(gone)
        )

    def test_the_producer_gives_each_thread_its_own_conversation(self) -> None:
        mail = FakeMailSource([
            arrival("2", "<quote@example.test>"),
            arrival("3", "<reply@example.test>",
                    references=["<quote@example.test>"]),
            arrival("4", "<invoice@example.test>"),
        ])
        source = self.source(mail)

        by_conversation: dict[str, list[str]] = {}
        for occasion in source.due_opportunities():
            by_conversation.setdefault(occasion.conversation_id, []).append(
                occasion.opportunity_id
            )

        self.assertEqual(len(by_conversation), 2, "two real threads")
        sizes = sorted(len(items) for items in by_conversation.values())
        self.assertEqual(sizes, [1, 2])

    def test_identity_survives_a_restart(self) -> None:
        """A new producer over the same observation derives the same thread."""
        event = arrival("2", "<quote@example.test>")

        first = self.source(FakeMailSource([event])).due_opportunities()[0]
        second = self.source(FakeMailSource([event])).due_opportunities()[0]

        self.assertEqual(first.conversation_id, second.conversation_id)

    def test_one_threads_goals_are_not_the_other_threads_state(self) -> None:
        """The property the mailbox-wide key broke, proved through the store."""
        from alx.contracts import (
            GoalState, Objective, SuccessCriterion,
        )
        from alx.goals import SQLiteGoalStore

        store = SQLiteGoalStore(Path(self.directory.name) / "goals.sqlite3")
        self.addCleanup(store.close)
        quote = mail_conversation_id(arrival("2", "<quote@example.test>"))
        invoice = mail_conversation_id(arrival("3", "<invoice@example.test>"))
        store.create(
            GoalState(
                "goal-1",
                Objective("event:mail:777:2", "Answer the quote"),
                (SuccessCriterion("criterion-1", "answered"),),
            ),
            quote,
            NOW.replace(year=2027),
        )

        own = store.list_unfinished(quote)
        self.assertEqual([item.goal_id for item in own], ["goal-1"])
        self.assertTrue(own[0].from_current_conversation)

        # Stage 2B made unfinished work visible across conversations, so the
        # invoice thread now sees this goal rather than being told it does not
        # exist. What isolation meant is preserved where it matters: the goal
        # is not the invoice thread's own, it says so, and it leads no list but
        # its own. Whether an unrelated thread's open work is worth anything
        # here is a judgement, and hiding it was how durable work became
        # unreachable in the first place.
        other = store.list_unfinished(invoice)
        self.assertEqual([item.goal_id for item in other], ["goal-1"])
        self.assertFalse(
            other[0].from_current_conversation,
            "another thread's goal must never look like this thread's own",
        )


class ReconciliationCannotStrandAClaimedOccasion(unittest.TestCase):
    """The window between the due snapshot and the turn, against real storage.

    `DueCognitionSource` snapshots what is due, claims it, and only then runs
    the turn. `MailPoller` reconciles on its own schedule, and a poll landing
    in that window used to settle the observation silently: it had never been
    shown to anyone, so nothing was owed. The already-issued occasion then ran
    with no mail event in context at all -- a synthetic `cognition.opportunity`
    for a message the Core could not see -- and the disappearance was never
    reported, leaving the observation settled while the ledger row stood
    claimed.

    Claiming now records the same durable `context_exposed` fact being shown a
    waiting item records, because both are a claim on her attention. Everything
    else follows from rules that already existed.
    """

    def setUp(self) -> None:
        from alx.providers import ICloudMailAdapter, SQLiteMailObservationState

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_mail_vertical_slice import FakeImap, message

        self.message = message
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.state = SQLiteMailObservationState(root / "observations.sqlite3")
        self.addCleanup(self.state.close)
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            self.state, 1, connection_factory=lambda *a, **k: self.imap,
        )
        self.ledger = SQLiteOpportunityLedger(root / "opportunities.sqlite3")
        self.addCleanup(self.ledger.close)
        self.source = MailCognitionSource(self.adapter, self.ledger, enabled=True)

    def _observe(self) -> None:
        self.adapter.scan()
        self.imap.items[2] = self.message("Quote", "R2,000 for the parts")
        self.adapter.scan()

    def _reconcile_away(self) -> None:
        """Exactly the poll that used to land in the window."""
        del self.imap.items[2]
        self.adapter.scan()

    def test_the_real_mail_event_still_reaches_the_core(self) -> None:
        self._observe()
        occasion = self.source.due_opportunities()[0]
        self.assertTrue(self.source.claim(occasion))

        self._reconcile_away()

        events = self.adapter.contextual_events()
        observed = [
            item for item in events if item.kind.startswith("mail.message")
        ]
        self.assertEqual(len(observed), 1, "the observation survived the race")
        self.assertEqual(observed[0].data["uid"], "2")

    def test_no_synthetic_only_turn_occurs(self) -> None:
        """A turn with no mail event is a turn reasoning about nothing."""
        self._observe()
        self.source.claim(self.source.due_opportunities()[0])

        self._reconcile_away()

        self.assertNotEqual(
            self.adapter.contextual_events(), (),
            "the turn would have seen only the synthetic occasion",
        )

    def test_the_disappearance_is_not_lost(self) -> None:
        self._observe()
        self.source.claim(self.source.due_opportunities()[0])

        self._reconcile_away()

        reported = self.state.pending_vanished()
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0].kind, "mail.message_vanished")
        self.assertEqual(reported[0].data["uid"], "2")

    def test_the_observation_is_not_settled_behind_the_claim(self) -> None:
        self._observe()
        self.source.claim(self.source.due_opportunities()[0])

        self._reconcile_away()

        state = self.state._connection.execute(
            "SELECT state FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertNotEqual(state, "done", "it was settled behind her back")

    def test_observation_and_ledger_agree_afterwards(self) -> None:
        """Neither is left describing a message the other has forgotten."""
        self._observe()
        occasion = self.source.due_opportunities()[0]
        self.source.claim(occasion)

        self._reconcile_away()

        self.assertTrue(self.ledger.exists(occasion.opportunity_id))
        live = self.state._connection.execute(
            "SELECT COUNT(*) FROM mail_observations WHERE uid = 2 "
            "AND state IN ('pending', 'current', 'presented')"
        ).fetchone()[0]
        self.assertEqual(live, 1, "the claim still has an observation behind it")

    def test_an_unreadable_body_does_not_fail_the_turn(self) -> None:
        """The message is gone, so why it cannot be read is the fact she gets."""
        self._observe()
        self.source.claim(self.source.due_opportunities()[0])

        self._reconcile_away()

        observed = [
            item for item in self.adapter.contextual_events()
            if item.kind.startswith("mail.message")
        ]
        self.assertIn("content_unavailable", observed[0].transient_data)

    def test_an_unclaimed_observation_is_still_settled_silently(self) -> None:
        """Nothing is owed for a message no occasion was ever raised about."""
        self._observe()

        self._reconcile_away()

        self.assertEqual(self.state.pending_vanished(), ())
        state = self.state._connection.execute(
            "SELECT state FROM mail_observations WHERE uid = 2"
        ).fetchone()[0]
        self.assertEqual(state, "done")


class AStaleOpportunityNeverReachesTheLedger(unittest.TestCase):
    """The mark is the precondition, not a courtesy.

    `claim` marked the observation and then threw the answer away. A snapshot
    taken before reconciliation and claimed after it therefore still got a
    ledger row: the observation was already settled, so the turn would have run
    on the synthetic occasion alone, and the row it left behind had to be
    reclaimed by a later recovery pass for a message that no longer existed.

    The order is now load-bearing. The observation must be durably marked
    before the ledger takes ownership, and a mark that reports no live
    observation refuses the claim outright. A stale occasion is not a failure
    needing replay -- it is an occasion that no longer exists -- so it leaves
    no row at all.
    """

    def setUp(self) -> None:
        from alx.providers import ICloudMailAdapter, SQLiteMailObservationState

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_mail_vertical_slice import FakeImap, message

        self.message = message
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.state = SQLiteMailObservationState(root / "observations.sqlite3")
        self.addCleanup(self.state.close)
        self.imap = FakeImap()
        self.adapter = ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            self.state, 1, connection_factory=lambda *a, **k: self.imap,
        )
        self.ledger = SQLiteOpportunityLedger(root / "opportunities.sqlite3")
        self.addCleanup(self.ledger.close)
        self.source = MailCognitionSource(self.adapter, self.ledger, enabled=True)

    def _stale_snapshot(self):
        """A due occasion whose observation is settled before it is claimed."""
        self.adapter.scan()
        self.imap.items[2] = self.message("Quote", "R2,000 for the parts")
        self.adapter.scan()
        occasion = self.source.due_opportunities()[0]
        # Reconciliation settles it silently: nothing was ever shown, so under
        # the store's own rule nothing is owed.
        del self.imap.items[2]
        self.adapter.scan()
        return occasion

    def test_the_observation_reports_that_it_is_gone(self) -> None:
        self._stale_snapshot()

        self.assertFalse(self.state.mark_claimed("mail:777:2"))

    def test_the_claim_is_refused(self) -> None:
        occasion = self._stale_snapshot()

        self.assertFalse(self.source.claim(occasion))

    def test_no_ledger_row_is_written(self) -> None:
        """Nothing to reclaim later, because nothing was ever owned."""
        occasion = self._stale_snapshot()

        self.source.claim(occasion)

        self.assertFalse(self.ledger.exists(occasion.opportunity_id))

    def test_the_core_is_never_invoked(self) -> None:
        """The runner stops at the refused claim, before any dispatch."""
        from alx.bootstrap.autonomous import AutonomousCognitionRunner

        class NeverCalled:
            def __init__(self) -> None:
                self.calls = []

            def receive_cognition_opportunity(self, *arguments):
                self.calls.append(arguments)
                raise AssertionError("the Core ran on a stale occasion")

        gateway = NeverCalled()
        occasion = self._stale_snapshot()
        runner = AutonomousCognitionRunner(
            self.source, self.ledger, gateway, 4, 3650, clock=lambda: NOW
        )

        ran = runner.run_one(occasion)

        self.assertFalse(ran, "a refused claim does not run")
        self.assertEqual(gateway.calls, [])

    def test_no_synthetic_only_turn_is_possible(self) -> None:
        """The whole point: a turn either has its observation or does not run."""
        occasion = self._stale_snapshot()

        claimed = self.source.claim(occasion)

        self.assertFalse(claimed)
        observed = [
            item for item in self.adapter.contextual_events()
            if item.kind.startswith("mail.message")
        ]
        self.assertEqual(
            observed, [],
            "the observation is genuinely gone, so no turn should have been "
            "claimed for it",
        )

    def test_a_live_observation_is_still_claimable(self) -> None:
        """The refusal must not catch ordinary occasions."""
        self.adapter.scan()
        self.imap.items[2] = self.message("Quote", "R2,000")
        self.adapter.scan()

        occasion = self.source.due_opportunities()[0]

        self.assertTrue(self.source.claim(occasion))
        self.assertTrue(self.ledger.exists(occasion.opportunity_id))

    def test_an_already_shown_observation_is_still_claimable(self) -> None:
        """Being shown a waiting item marks it too; that is not staleness.

        Reporting "already marked" as failure would refuse an occasion for
        every message she had already been shown, which is most of them.
        """
        self.adapter.scan()
        self.imap.items[2] = self.message("Quote", "R2,000")
        self.adapter.scan()
        self.adapter.contextual_events()  # a turn builds context, marking it

        occasion = self.source.due_opportunities()[0]

        self.assertTrue(
            self.state.mark_claimed("mail:777:2"),
            "the observation is live",
        )
        self.assertTrue(self.source.claim(occasion))

    def test_a_mark_that_raises_still_fails_closed(self) -> None:
        """Preserved: an unrecordable claim is refused, not taken."""
        class Exploding:
            def unclaimed_arrivals(self):
                return (arrival(),)

            def pending_vanished(self):
                return ()

            def mark_claimed(self, event_id):
                raise RuntimeError("observation store unavailable")

        source = MailCognitionSource(Exploding(), self.ledger, enabled=True)
        occasion = source.due_opportunities()[0]

        self.assertFalse(source.claim(occasion))
        self.assertFalse(self.ledger.exists(occasion.opportunity_id))


class AHeaderlessMessageKeepsOneConversation(Fixture):
    """An arrival and its disappearance are one message, so one thread.

    The fallback used the event id, and one message produces two events. The
    disappearance therefore landed in a conversation that had never heard of
    the message, while the thread that raised it waited for an answer that
    arrived somewhere else.
    """

    def _headerless(self, uid: str, *, gone: bool = False) -> BackgroundEvent:
        data = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": uid}
        if gone:
            return BackgroundEvent(
                f"mail:777:{uid}:vanished", "mail.message_vanished", NOW, data
            )
        return BackgroundEvent(
            f"mail:777:{uid}", "mail.message_arrived", NOW, data
        )

    def test_arrival_and_disappearance_share_one_conversation(self) -> None:
        arrived = self._headerless("2")
        gone = self._headerless("2", gone=True)

        self.assertEqual(
            mail_conversation_id(arrived), mail_conversation_id(gone)
        )

    def test_the_identity_names_the_observation_not_the_variant(self) -> None:
        gone = self._headerless("2", gone=True)

        self.assertEqual(mail_conversation_id(gone), "mail-thread:mail:777:2")

    def test_different_headerless_messages_stay_separate(self) -> None:
        """Normalising the variant must not merge unrelated messages."""
        first = self._headerless("2")
        second = self._headerless("3")

        self.assertNotEqual(
            mail_conversation_id(first), mail_conversation_id(second)
        )
        self.assertNotEqual(
            mail_conversation_id(self._headerless("2", gone=True)),
            mail_conversation_id(self._headerless("3", gone=True)),
        )

    def test_identity_survives_restart_and_reconciliation(self) -> None:
        """Rebuilt from the mailbox coordinates, which the row keeps."""
        before = mail_conversation_id(self._headerless("2"))
        after = mail_conversation_id(self._headerless("2", gone=True))

        self.assertEqual(before, after)
        # And again from a freshly constructed event, as a restart would.
        self.assertEqual(before, mail_conversation_id(self._headerless("2")))

    def test_rfc_threading_is_unchanged(self) -> None:
        """The hierarchy above the fallback still decides when it can."""
        root = arrival("2", "<a@example.test>")
        reply = arrival(
            "3", "<b@example.test>",
            in_reply_to="<a@example.test>",
            references=["<a@example.test>"],
        )

        self.assertEqual(mail_conversation_id(root), "mail-thread:<a@example.test>")
        self.assertEqual(mail_conversation_id(reply), mail_conversation_id(root))

    def test_a_threaded_disappearance_still_uses_its_headers(self) -> None:
        """The fallback applies only when there are no identifiers at all."""
        gone = vanished("2", "<a@example.test>")

        self.assertEqual(mail_conversation_id(gone), "mail-thread:<a@example.test>")

    def test_the_producer_keeps_both_facts_in_one_thread(self) -> None:
        mail = FakeMailSource(
            [self._headerless("2")], [self._headerless("2", gone=True)]
        )
        source = self.source(mail)

        conversations = {
            occasion.conversation_id
            for occasion in source.due_opportunities()
        }

        self.assertEqual(len(conversations), 1)

    def test_they_remain_two_occasions_in_that_one_thread(self) -> None:
        """Sharing a conversation must not merge the occasions themselves."""
        mail = FakeMailSource(
            [self._headerless("2")], [self._headerless("2", gone=True)]
        )
        source = self.source(mail)

        identities = {
            occasion.opportunity_id
            for occasion in source.due_opportunities()
        }

        self.assertEqual(len(identities), 2)


class TheComposedRuntimeIncludesMail(unittest.TestCase):
    """The composition root actually wires mail into the process-lifetime tick.

    Every other test here builds the producer directly. This one reads the
    composition root itself, because a correct producer nothing composes is a
    correct producer that never runs.
    """

    SOURCE = Path(__file__).resolve().parents[1] / "src" / "alx"

    def test_mail_joins_the_combined_occasion_source(self) -> None:
        composition = (self.SOURCE / "bootstrap" / "live_voice.py").read_text()

        self.assertIn("MailCognitionSource(", composition)
        self.assertIn("occasion_sources.append(mail_cognition_source)", composition)
        self.assertIn("CombinedOccasionSource(*occasion_sources)", composition)

    def test_the_combined_source_feeds_the_one_process_lifetime_tick(self) -> None:
        composition = (self.SOURCE / "bootstrap" / "live_voice.py").read_text()

        self.assertIn("DueCognitionSource(\n        occasion_source,", composition)
        self.assertIn("runtime_tasks.create_task(due_cognition.run())", composition)

    def test_mail_recovery_runs_before_the_tick_starts(self) -> None:
        """No occasion may be offered from a half-recovered ledger."""
        composition = (self.SOURCE / "bootstrap" / "live_voice.py").read_text()

        recovery = composition.index("mail_cognition_source.recover(")
        tick = composition.index("runtime_tasks.create_task(due_cognition.run())")
        self.assertLess(recovery, tick)

    def test_the_voice_session_is_not_given_an_event_source(self) -> None:
        """Presentation only: mail no longer enters through the transport."""
        composition = (self.SOURCE / "bootstrap" / "live_voice.py").read_text()

        self.assertNotIn("event_source=", composition)

    def test_mail_is_gated_by_the_same_master_switch(self) -> None:
        composition = (self.SOURCE / "bootstrap" / "live_voice.py").read_text()
        block = composition[composition.index("mail_cognition_source = "):]
        block = block[: block.index(")")]

        self.assertIn("enabled=providers.autonomous is not None", block)

    def test_the_composed_source_really_produces_mail_occasions(self) -> None:
        """Not only wired: the combined source yields what mail found."""
        from alx.continuity.occasions import CombinedOccasionSource

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = SQLiteOpportunityLedger(
            Path(directory.name) / "opportunities.sqlite3"
        )
        self.addCleanup(ledger.close)
        mail = FakeMailSource([arrival()])
        combined = CombinedOccasionSource(
            MailCognitionSource(mail, ledger, enabled=True)
        )

        occasions = combined.due_opportunities()

        self.assertEqual(len(occasions), 1)
        self.assertIs(occasions[0].origin, CognitionOrigin.EXTERNAL_EVENT)
        self.assertTrue(combined.claim(occasions[0]))


if __name__ == "__main__":
    unittest.main()
