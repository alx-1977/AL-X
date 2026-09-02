"""AL/X selects which goal an input belongs to; code only validates it.

The live defect: a research goal was waiting for input when Friedl asked for a
message to be moved to Trash. The runtime attached the newest unfinished goal
before the Core reasoned, so the deletion was bound to the research goal, the
effectful call was refused, and the same impossible state was reasoned from
until the step budget ran out. The approval Friedl gave was recorded on the
research goal on the way, so the next attempt was refused for reusing it.

Nothing here names mail or research. The capabilities are generic effectful
primitives and the goals are generic pieces of unfinished work.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalLifecycle, ApprovalProposal, ApprovalScope,
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationSnapshot, ConversationTurn,
    GoalMutationKind, GoalProposal, GoalState, GoalStatus, GoalStopReason,
    ContentOrigin, MemoryKind, MemoryProposal,
    Objective, RetentionPolicy, SideEffect, StructuredSchema, SuccessCriterion,
    ValueKind, WorkItem,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.memories import SQLiteMemoryStore  # noqa: E402
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402

NOW = datetime(2026, 9, 2, 10, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
REMOVE = CapabilityDefinition(
    "remove_item", "Remove one identified item", SCHEMA, SCHEMA,
    SideEffect.EFFECTFUL,
)
STUDY = CapabilityDefinition(
    "study_question", "Answer one bounded question", SCHEMA, SCHEMA,
    SideEffect.EFFECTFUL,
)
PERMISSION = "items.remove"
STUDY_PERMISSION = "questions.study"
ARGUMENTS = {"item_id": "item-7"}

TURNS = (
    ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                     "Please start the longer piece of work.", NOW, "friedl"),
    ConversationTurn("conversation-1", "turn-2", ConversationOrigin.ALX_RESPONSE,
                     "I need one more detail before I can continue.",
                     NOW + timedelta(seconds=5)),
    ConversationTurn("conversation-1", "turn-3", ConversationOrigin.TYPED,
                     "Never mind that for now. Remove item seven, yes, go ahead.",
                     NOW + timedelta(minutes=1), "friedl"),
)


def conversation(*turns: ConversationTurn) -> ConversationSnapshot:
    return ConversationSnapshot("conversation-1", turns or TURNS, 3, RETENTION)


def paused_goal(goal_id: str = "goal-a") -> GoalState:
    return GoalState(
        goal_id,
        Objective("turn:turn-1", "The longer piece of work"),
        (SuccessCriterion("a-1", "the longer work is done"),),
        outstanding_work=(WorkItem("a-work", "one detail still to be supplied"),),
        status=GoalStatus.AWAITING_INPUT,
        stop_reason=GoalStopReason.REQUIRED_INPUT,
    )


def active_goal(goal_id: str = "goal-a") -> GoalState:
    return GoalState(
        goal_id,
        Objective("turn:turn-1", "The longer piece of work"),
        (SuccessCriterion("a-1", "the longer work is done"),),
    )


def removal(call_id: str = "call-1") -> CapabilityCall:
    return CapabilityCall(call_id, "remove_item", ARGUMENTS, "approval-1")


def approval() -> ApprovalProposal:
    return ApprovalProposal(
        "approval-1", ApprovalScope("remove_item", ARGUMENTS), "turn:turn-3",
    )


def new_goal(summary: str = "Remove item seven") -> GoalProposal:
    return GoalProposal(
        GoalMutationKind.CREATE, summary,
        (SuccessCriterion("b-1", "item seven is removed"),),
    )


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Broker:
    """The real safety gate behind a recording dispatch."""

    def __init__(self) -> None:
        self.calls: list[tuple[CapabilityCall, GoalState | None]] = []
        self.executed = 0
        self._gate = SafetyGate({
            "remove_item": AuthorityPolicy(
                frozenset({PERMISSION}), approval_required=True),
            "study_question": AuthorityPolicy(frozenset({STUDY_PERMISSION})),
        })

    def __call__(self, call: CapabilityCall, state: GoalState | None) -> CapabilityAttempt:
        self.calls.append((call, state))
        verdict = self._gate.evaluate(
            call,
            AuthorityContext(
                "friedl", frozenset({PERMISSION, STUDY_PERMISSION}), NOW,
                approvals=() if state is None else state.approvals,
            ),
        )
        if not verdict.allowed:
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.REJECTED, False,
                reason_code=verdict.reason,
            )
        self.executed += 1
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(
                call.call_id, call.capability_id, CapabilityResultState.SUCCEEDED,
                {"done": True},
            ),
        )


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.broker = Broker()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def agent(self, reasoner) -> CoreAgent:
        identifiers = iter(("goal-b", "goal-c", "goal-d"))
        return CoreAgent(
            self.store, reasoner, self.broker, (REMOVE, STUDY),
            memory_store=SQLiteMemoryStore(self.root / "memories.sqlite3"),
            clock=lambda: NOW, identifier_factory=lambda: next(identifiers),
        )


class IndependentGoalTests(Fixture):
    """Two unrelated unfinished goals coexist without contaminating each other."""

    def test_an_active_goal_does_not_block_an_independent_new_one(self) -> None:
        """The exact incident, generalised: active long-running work, new request."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        reasoner = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        # The first goal is byte-for-byte untouched.
        self.assertEqual(self.store.load("goal-a"), before)
        self.assertEqual(self.store.load("goal-a").state.status, GoalStatus.ACTIVE)
        created = self.store.load("goal-b").state
        self.assertEqual(created.objective.source_reference, "turn:turn-3")
        self.assertEqual(self.broker.calls[0][1].goal_id, "goal-b")

    def test_an_active_goal_of_one_kind_does_not_block_another_kind(self) -> None:
        """Neither direction: the second goal is independent of the first."""
        self.store.create(active_goal("goal-a"), "conversation-1", RETENTION)
        study = CapabilityCall("call-1", "study_question", {"question_id": "q-1"})
        reasoner = Queued(
            AgentDecision(
                call=study,
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE, "Answer the bounded question",
                    (SuccessCriterion("c-1", "the question is answered"),),
                ),
            ),
            AgentDecision(response="Answered.", goal_id="goal-b"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        self.assertEqual(self.store.load("goal-a").state.status, GoalStatus.ACTIVE)
        self.assertEqual(
            {item.state.goal_id for item in self.store.list_goals()},
            {"goal-a", "goal-b"},
        )

    def test_a_paused_goal_is_untouched_by_unrelated_work(self) -> None:
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        reasoner = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(self.store.load("goal-a"), before)
        self.assertEqual(
            self.store.load("goal-a").state.status, GoalStatus.AWAITING_INPUT
        )
        self.assertEqual(self.store.load("goal-a").state.approvals, ())

    def test_the_core_selects_an_existing_goal_rather_than_duplicating_it(self) -> None:
        """A follow-up that belongs to unfinished work continues it."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-a",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE,
                    objective_summary="The longer piece of work, refined",
                ),
            ),
            AgentDecision(response="Carrying on with that.", goal_id="goal-a"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(len(self.store.list_goals()), 1)
        self.assertEqual(
            self.store.load("goal-a").state.objective.summary,
            "The longer piece of work, refined",
        )

    def test_ordinary_conversation_still_needs_no_goal(self) -> None:
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(AgentDecision(response="Hello."))
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(len(self.store.list_goals()), 1)


class GoalSummaryTests(Fixture):
    """What the Core sees before it chooses, and what it must ask for."""

    def test_every_unfinished_goal_is_offered_and_none_is_preselected(self) -> None:
        self.store.create(paused_goal("goal-a"), "conversation-1", RETENTION)
        self.store.create(active_goal("goal-x"), "conversation-1", RETENTION)
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        context = reasoner.contexts[0]
        self.assertIsNone(context.active_goal, "nothing may preselect a goal")
        self.assertEqual(
            [item.goal_id for item in context.unfinished_goals], ["goal-a", "goal-x"]
        )

    def test_a_summary_identifies_a_goal_without_carrying_its_history(self) -> None:
        state = replace(
            active_goal(),
            attempts=(
                CapabilityAttempt(
                    CapabilityCall("old-1", "remove_item", {"item_id": "item-1"}),
                    CapabilityAttemptDisposition.EXECUTED, True,
                    CapabilityResult(
                        "old-1", "remove_item", CapabilityResultState.SUCCEEDED,
                        {"done": True},
                    ),
                ),
            ),
        )
        self.store.create(state, "conversation-1", RETENTION)
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        summary = reasoner.contexts[0].unfinished_goals[0]
        self.assertEqual(summary.goal_id, "goal-a")
        self.assertEqual(summary.objective_summary, "The longer piece of work")
        self.assertEqual(summary.status, GoalStatus.ACTIVE)
        # A summary carries no attempts, evidence, approvals or referents.
        for absent in ("attempts", "evidence", "approvals", "referents", "context"):
            self.assertFalse(hasattr(summary, absent), absent)

    def test_only_the_selected_goal_enters_reasoning_in_full(self) -> None:
        self.store.create(active_goal("goal-a"), "conversation-1", RETENTION)
        self.store.create(active_goal("goal-x"), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(goal_id="goal-a"),
            AgentDecision(response="Carrying on.", goal_id="goal-a"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        # Before selection: summaries only.
        self.assertIsNone(reasoner.contexts[0].active_goal)
        # After selection: exactly one goal in full, and it is the chosen one.
        self.assertEqual(reasoner.contexts[1].active_goal.goal_id, "goal-a")
        self.assertEqual(len(reasoner.contexts[1].unfinished_goals), 2)

    def test_a_goal_from_another_conversation_is_never_offered(self) -> None:
        self.store.create(active_goal("goal-a"), "conversation-1", RETENTION)
        self.store.create(active_goal("goal-z"), "another-conversation", RETENTION)
        reasoner = Queued(AgentDecision(response="Noted."))
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(
            [item.goal_id for item in reasoner.contexts[0].unfinished_goals],
            ["goal-a"],
        )

    def test_a_selection_of_an_unknown_goal_is_refused(self) -> None:
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(response="Working on it.", goal_id="goal-invented"),
            AssertionError("a paid retry occurred"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_selection_unknown")
        self.assertEqual(len(reasoner.contexts), 1)

    def test_asking_twice_for_the_same_goal_is_stopped(self) -> None:
        """Inspection is a step toward acting, never a way to spend the budget."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(goal_id="goal-a"),
            AgentDecision(goal_id="goal-a"),
            AssertionError("a third decision occurred"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.reason, "goal_selection_redundant")
        self.assertEqual(len(reasoner.contexts), 2)


class SelectionUsesTheCanonicalPathTests(Fixture):
    """Reading a goal is a step, not a second way into durable state.

    Review finding: select_goal had its own reduce-and-persist branch that ran
    before memory grounding and before the selected goal's provenance was
    known. An objective change could therefore be committed by a turn that
    then failed, and a mail-derived goal could raise on provenance.
    """

    def test_a_failing_memory_proposal_leaves_the_objective_unchanged(self) -> None:
        """The adversarial case: persisted mutation, then a failed turn."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        ungrounded = MemoryProposal(
            "memory-1", MemoryKind.FACTUAL, "unsupported", ("turn:not-real",), NOW,
        )
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-a",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE, objective_summary="a changed objective",
                ),
                memory_proposals=(ungrounded,),
            ),
            AssertionError("a paid retry occurred"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_proposal_invalid")
        self.assertEqual(self.store.load("goal-a"), before)
        self.assertEqual(
            self.store.load("goal-a").state.objective.summary,
            "The longer piece of work",
        )

    def test_the_selected_goal_is_inside_the_provenance_it_is_persisted_with(self) -> None:
        """A goal carrying external provenance must not be dropped from the union."""
        mail_provenance = RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, NOW)
        self.store.create(
            active_goal(), "conversation-1", RETENTION, mail_provenance,
        )
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-a",
                goal_proposal=GoalProposal(
                    GoalMutationKind.UPDATE, objective_summary="refined objective",
                ),
            ),
            AgentDecision(response="Carrying on.", goal_id="goal-a"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        stored = self.store.load("goal-a")
        self.assertEqual(stored.state.objective.summary, "refined objective")
        # The goal's own origins survived into what replaced it.
        self.assertIn(ContentOrigin.EXTERNAL, stored.provenance.origins)

    def test_a_bare_reading_step_persists_nothing(self) -> None:
        self.store.create(active_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        reasoner = Queued(
            AgentDecision(goal_id="goal-a"),
            AgentDecision(response="Carrying on.", goal_id="goal-a"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(self.store.load("goal-a").revision, before.revision)


class SelectionCannotBuyReasoningTests(Fixture):
    """Review finding: distinct goals each purchased another reasoning call."""

    def create_goals(self, count: int) -> None:
        for index in range(count):
            self.store.create(
                active_goal(f"goal-{index}"), "conversation-1", RETENTION,
            )

    def test_walking_the_conversation_goals_is_refused_after_the_first(self) -> None:
        self.create_goals(4)
        reasoner = Queued(
            AgentDecision(goal_id="goal-0"),
            AgentDecision(goal_id="goal-1"),
            AssertionError("a third selection bought another reasoning call"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "goal_selection_exhausted")
        self.assertEqual(len(reasoner.contexts), 2)

    def test_the_step_budget_is_never_reached_by_selection_alone(self) -> None:
        """Twenty-five goals, twenty-five steps, two decisions."""
        self.create_goals(25)
        reasoner = Queued(
            *[AgentDecision(goal_id=f"goal-{index}") for index in range(25)]
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.reason, "goal_selection_exhausted")
        self.assertLessEqual(len(reasoner.contexts), 2)

    def test_acting_on_the_one_selected_goal_is_unaffected(self) -> None:
        """The cap limits moving between goals, never working within one."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        self.store.create(active_goal("goal-other"), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "study_question", {"question_id": "q-1"})
        reasoner = Queued(
            AgentDecision(goal_id="goal-a"),
            AgentDecision(call=call, goal_id="goal-a"),
            AgentDecision(response="Answered.", goal_id="goal-a"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        self.assertEqual(len(reasoner.contexts), 3)

    def test_creating_a_goal_after_reading_one_is_still_allowed(self) -> None:
        """Reading unfinished work, then judging it unrelated, must not deadlock."""
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(goal_id="goal-a"),
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        self.assertEqual(self.store.load("goal-a").state.approvals, ())


class BlockedDispatchTests(Fixture):
    """An impossible dispatch costs one decision and never a retry loop."""

    def assert_one_decision_then_checkpoint(self, reasoner) -> None:
        outcome = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "active_goal_required")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(self.broker.calls, [])

    def test_effectful_call_against_a_selected_paused_goal_is_one_decision(self) -> None:
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(call=removal(), goal_id="goal-a",
                          approval_proposal=approval()),
            AssertionError("a paid retry occurred against the same state"),
        )
        self.assert_one_decision_then_checkpoint(reasoner)

    def test_effectful_call_without_any_goal_is_one_decision(self) -> None:
        reasoner = Queued(
            AgentDecision(call=removal(), approval_proposal=approval()),
            AssertionError("a paid retry occurred against the same state"),
        )
        self.assert_one_decision_then_checkpoint(reasoner)
        self.assertEqual(self.store.list_goals(), ())

    def test_a_mutation_that_pauses_the_goal_cannot_carry_an_effectful_call(self) -> None:
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(
                call=removal(), goal_id="goal-a",
                goal_proposal=GoalProposal(
                    GoalMutationKind.AWAIT_INPUT,
                    outstanding_work=(WorkItem("w-1", "waiting"),),
                ),
                approval_proposal=approval(),
            ),
            AssertionError("a paid retry occurred"),
        )
        self.assert_one_decision_then_checkpoint(reasoner)
        self.assertEqual(self.store.load("goal-a").state.status, GoalStatus.ACTIVE)

    def test_the_step_budget_is_not_the_protection(self) -> None:
        """Twenty-five steps were available; one was used."""
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(call=removal(), goal_id="goal-a",
                          approval_proposal=approval()),
            *([AssertionError("retry")] * 24),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(len(reasoner.contexts), 1)

    def test_the_transport_keeps_listening_after_a_blocked_dispatch(self) -> None:
        from alx.interfaces.server import (
            MID_EXCHANGE_RECOVERABLE_REASONS, RECOVERABLE_TRANSPORT_REASONS,
        )

        self.assertIn("active_goal_required", RECOVERABLE_TRANSPORT_REASONS)
        self.assertIn("active_goal_required", MID_EXCHANGE_RECOVERABLE_REASONS)


class MemoryFaultDoesNotEndTheConversationTests(Fixture):
    """A storage fault is the last resort, not the first line of defence.

    Friedl was mid-sentence about his dogs when a memory write failed and the
    session closed. The write itself is fixed upstream: a reused identifier is
    now idempotent and the protocol states the rule. What remains here is
    storage genuinely failing, where losing the conversation is worse than
    reporting the fault and listening on.
    """

    class BrokenMemoryStore:
        """A memory store that cannot write, as a full disk would behave."""

        def remember_many(self, proposals, retention_until):
            raise OSError("database or disk is full")

        def remember(self, proposal, retention_until):
            raise OSError("database or disk is full")

        def retrieve(self, query, as_of):
            return ()

    def failing_agent(self, reasoner) -> CoreAgent:
        return CoreAgent(
            self.store, reasoner, self.broker, (REMOVE, STUDY),
            memory_store=self.BrokenMemoryStore(),
            clock=lambda: NOW, identifier_factory=lambda: "goal-b",
        )

    def memory(self) -> MemoryProposal:
        # Grounded in a real person turn, formed no earlier than that turn and
        # no later than the Core's clock, so it reaches storage rather than
        # being refused at grounding.
        return MemoryProposal(
            "mem-1", MemoryKind.RELATIONSHIP, "Friedl has two dogs.",
            ("turn:turn-1",), NOW, "friedl",
        )

    def test_a_failed_memory_write_reports_the_fault_without_acting(self) -> None:
        self.store.create(active_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        reasoner = Queued(
            AgentDecision(response="Noted.", goal_id="goal-a",
                          memory_proposals=(self.memory(),)),
            AssertionError("a paid retry occurred"),
        )
        outcome = self.failing_agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "memory_persistence_error")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(self.broker.calls, [], "nothing external may act")
        self.assertEqual(self.store.load("goal-a").state, before.state)

    def test_the_transport_keeps_listening_after_a_memory_fault(self) -> None:
        from alx.interfaces.server import (
            MID_EXCHANGE_RECOVERABLE_REASONS, RECOVERABLE_TRANSPORT_REASONS,
        )

        self.assertIn("memory_persistence_error", RECOVERABLE_TRANSPORT_REASONS)
        self.assertIn("memory_persistence_error", MID_EXCHANGE_RECOVERABLE_REASONS)

    def test_a_later_turn_still_works_once_storage_recovers(self) -> None:
        """The conversation survives the fault rather than ending on it."""
        self.store.create(active_goal(), "conversation-1", RETENTION)
        failed = Queued(
            AgentDecision(response="Noted.", goal_id="goal-a",
                          memory_proposals=(self.memory(),)),
        )
        self.failing_agent(failed).process(conversation(), RETENTION, 25)

        recovered = Queued(
            AgentDecision(response="Still here.", goal_id="goal-a",
                          memory_proposals=(self.memory(),)),
        )
        outcome = self.agent(recovered).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Still here.")

    def test_a_checkpointed_dispatch_is_never_repeated_after_the_fault(self) -> None:
        """The one site that fails after a durable checkpoint.

        The approval is claimed and the attempt recorded PENDING just before
        dispatch. If the memory write fails there, nothing was sent, and the
        next turn must close that attempt as an unknown outcome rather than
        dispatching it a second time.
        """
        self.store.create(active_goal(), "conversation-1", RETENTION)
        call = CapabilityCall("call-1", "study_question", {"question_id": "q-1"})
        reasoner = Queued(
            AgentDecision(call=call, goal_id="goal-a",
                          memory_proposals=(self.memory(),)),
        )
        outcome = self.failing_agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.reason, "memory_persistence_error")
        self.assertEqual(self.broker.executed, 0, "nothing was dispatched")

        # The next turn, with storage working again.
        resumed = Queued(AgentDecision(response="I could not confirm that.",
                                       goal_id="goal-a"))
        self.agent(resumed).process(conversation(), RETENTION, 25)
        self.assertEqual(self.broker.executed, 0, "the action must not be repeated")
        attempts = self.store.load("goal-a").state.attempts
        self.assertFalse(
            any(item.disposition is CapabilityAttemptDisposition.PENDING
                for item in attempts),
            "the interrupted dispatch must be resolved, not left pending",
        )


class ApprovalOrderingTests(Fixture):
    """Eligibility, then approval, then dispatch, then consumption."""

    def test_an_ineligible_goal_does_not_record_or_consume_the_approval(self) -> None:
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        reasoner = Queued(
            AgentDecision(call=removal(), goal_id="goal-a",
                          approval_proposal=approval()),
            AssertionError("retry"),
        )
        self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(self.store.load("goal-a"), before)
        self.assertEqual(self.store.load("goal-a").state.approvals, ())
        self.assertEqual(len(self.store.list_goals()), 1)

    def test_the_approval_remains_usable_once_the_goal_is_corrected(self) -> None:
        """The same approval, from the same turn, on the next reasoning pass."""
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        blocked = Queued(
            AgentDecision(call=removal(), goal_id="goal-a",
                          approval_proposal=approval())
        )
        self.agent(blocked).process(conversation(), RETENTION, 25)

        corrected = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        outcome = self.agent(corrected).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        _call, authority = self.broker.calls[0]
        self.assertEqual(authority.approvals[0].approval_id, "approval-1")
        self.assertEqual(authority.approvals[0].lifecycle, ApprovalLifecycle.GRANTED)
        self.assertEqual(
            self.store.load("goal-b").state.approvals[0].lifecycle,
            ApprovalLifecycle.CONSUMED,
        )

    def test_one_successful_action_still_cannot_reuse_the_approval(self) -> None:
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        first = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        self.agent(first).process(conversation(), RETENTION, 25)
        self.assertEqual(self.broker.executed, 1)

        again = Queued(
            AgentDecision(call=removal("call-2"), goal_id="goal-b"),
            AgentDecision(response="No.", goal_id="goal-b"),
        )
        outcome = self.agent(again).process(conversation(), RETENTION, 25)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        repeated = self.store.load("goal-b").state.attempts[-1]
        self.assertEqual(repeated.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(repeated.reason_code, "approval_invalid")
        self.assertEqual(
            self.store.load("goal-b").state.approvals[0].lifecycle,
            ApprovalLifecycle.CONSUMED,
        )


class NoDeterministicRouterTests(unittest.TestCase):
    """Law 1: no production code chooses the goal."""

    def test_no_module_selects_a_goal_by_recency_domain_or_wording(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "alx"
        superseded = (
            "locate_active_goal", "ActiveGoalLocator", "latest_goal",
            "most_recent_goal", "goal_for_capability", "goal_for_domain",
        )
        for path in root.rglob("*.py"):
            source = path.read_text("utf-8")
            for name in superseded:
                self.assertNotIn(
                    name, source, f"{path.name} still routes to a goal deterministically"
                )

    def test_the_gateway_cannot_be_given_a_goal_or_a_locator(self) -> None:
        import inspect

        from alx.conversation import ConversationGateway

        parameters = set(inspect.signature(ConversationGateway.__init__).parameters)
        self.assertNotIn("locate_active_goal", parameters)
        self.assertFalse({item for item in parameters if "goal" in item})

    def test_the_core_cannot_be_handed_a_preselected_goal(self) -> None:
        import inspect

        parameters = set(inspect.signature(CoreAgent.process).parameters)
        self.assertNotIn("goal_id", parameters)

    def test_the_store_orders_goals_but_never_chooses_one(self) -> None:
        """`list_unfinished` returns them all; picking one is not its job."""
        import inspect

        from alx.goals import SQLiteGoalStore

        source = inspect.getsource(SQLiteGoalStore.list_unfinished)
        for choosing in ("[-1]", "[0]", "LIMIT 1", "max(", "min("):
            self.assertNotIn(choosing, source)


class RunawayScenarioTests(unittest.TestCase):
    """The exact incident shape, end to end through the gateway."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.goals = SQLiteGoalStore(self.root / "goals.sqlite3")
        self.conversations = SQLiteConversationStore(self.root / "conversations.sqlite3")
        self.broker = Broker()
        self.goals.create(paused_goal(), "conversation-1", RETENTION)
        snapshot = self.conversations.create("conversation-1", RETENTION)
        for turn in TURNS[:2]:
            snapshot = self.conversations.append(turn, RETENTION, snapshot.revision)

    def tearDown(self) -> None:
        self.conversations.close()
        self.goals.close()
        self.directory.cleanup()

    def gateway(self, reasoner) -> ConversationGateway:
        identifiers = iter(("goal-b", "goal-c"))
        core = CoreAgent(
            self.goals, reasoner, self.broker, (REMOVE, STUDY),
            clock=lambda: NOW, identifier_factory=lambda: next(identifiers),
        )
        responses = iter(("alx-1", "alx-2"))
        return ConversationGateway(
            core, self.conversations,
            identifier_factory=lambda: next(responses), clock=lambda: NOW,
        )

    def test_a_correct_context_dispatches_exactly_once(self) -> None:
        before = self.goals.load("goal-a")
        reasoner = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        outcome = self.gateway(reasoner).receive_conversation_turn(TURNS[2], 25, RETENTION)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        # The paused goal was offered, not imposed.
        self.assertIsNone(reasoner.contexts[0].active_goal)
        self.assertEqual(
            [item.goal_id for item in reasoner.contexts[0].unfinished_goals], ["goal-a"]
        )
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.broker.executed, 1)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(self.goals.load("goal-a"), before)
        self.assertEqual(
            self.goals.load("goal-b").state.approvals[0].lifecycle,
            ApprovalLifecycle.CONSUMED,
        )

    def test_no_eligible_context_stops_after_one_decision_with_the_approval_unconsumed(self) -> None:
        before = self.goals.load("goal-a")
        reasoner = Queued(
            AgentDecision(call=removal(), goal_id="goal-a",
                          approval_proposal=approval()),
            *([AssertionError("a paid retry occurred")] * 24),
        )
        outcome = self.gateway(reasoner).receive_conversation_turn(TURNS[2], 25, RETENTION)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "active_goal_required")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.goals.load("goal-a"), before)
        self.assertTrue(
            all(not snapshot.state.approvals for snapshot in self.goals.list_goals())
        )
        durable = self.conversations.load("conversation-1").turns[-1]
        self.assertEqual(
            (durable.turn_id, durable.content), (TURNS[2].turn_id, TURNS[2].content)
        )

    def test_restart_preserves_every_unfinished_goal_and_their_independence(self) -> None:
        """Two independent goals, a restart, and both still resumable."""
        first = Queued(
            AgentDecision(call=removal(), goal_proposal=new_goal(),
                          approval_proposal=approval()),
            AgentDecision(response="Removed.", goal_id="goal-b"),
        )
        self.gateway(first).receive_conversation_turn(TURNS[2], 25, RETENTION)
        self.goals.close()

        reopened = SQLiteGoalStore(self.root / "goals.sqlite3")
        try:
            summaries = reopened.list_unfinished("conversation-1")
            self.assertEqual(
                [item.goal_id for item in summaries], ["goal-a", "goal-b"]
            )
            self.assertEqual(summaries[0].status, GoalStatus.AWAITING_INPUT)
            self.assertEqual(summaries[0].outstanding_work,
                             ("one detail still to be supplied",))
            self.assertEqual(summaries[1].status, GoalStatus.ACTIVE)
            # The paused goal never absorbed the other one's work.
            self.assertEqual(reopened.load("goal-a").state.attempts, ())
            self.assertEqual(reopened.load("goal-a").state.approvals, ())
            self.assertEqual(len(reopened.load("goal-b").state.attempts), 1)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
