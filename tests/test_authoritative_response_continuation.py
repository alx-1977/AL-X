"""Goal selection with proposals must continue, never impersonate an answer."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision, ConversationOrigin, GoalMutationKind, GoalProposal,
    MemoryKind, MemoryProposal,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore
from alx.core.loop import CoreOutcome, CoreState
from alx.interfaces import VoiceEventKind, VoiceSession
from alx.memories import SQLiteMemoryStore
from test_goal_context_selection import (
    Fixture, Queued, RETENTION, TURNS, active_goal, paused_goal, conversation,
    removal, new_goal, approval,
)
from test_live_voice import FakeSynthesizer, FakeTranscriber, incoming_audio
from alx.contracts import TranscriptionEvent, TranscriptionState


class SelectionContinuationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.memory = SQLiteMemoryStore(self.root / "selection-memories.sqlite3")
        self.addCleanup(self.memory.close)
        self.now = TURNS[-1].occurred_at

    def core(self, reasoner):
        core = self.agent(reasoner)
        core._memory_store.close()
        core._memory_store = self.memory
        core._clock = lambda: self.now
        return core

    def preference(self, content="Keep operational replies concise."):
        return MemoryProposal(
            "preference-1", MemoryKind.RELATIONSHIP, content,
            ("turn:turn-3",), self.now, person_id="friedl",
        )

    def selection(self, *, memory=True, mutation=False):
        """Select goal-a, then each write as its own step (D-065)."""
        steps = [AgentDecision(goal_id="goal-a")]
        if mutation:
            steps.append(AgentDecision(goal_proposal=GoalProposal(
                GoalMutationKind.UPDATE, objective_summary="refined objective")))
        if memory:
            steps.append(AgentDecision(memory_proposals=(self.preference(),)))
        return steps

    def test_memory_selection_persists_once_then_answers(self):
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(), AgentDecision(response="Understood."))
        result = self.core(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "Understood.")
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual(len(self.memory.load("preference-1").revisions), 1)
        self.assertEqual(self.store.load("goal-a").revision, 2)
        self.assertEqual(self.broker.executed, 0)

    def test_combined_selection_persists_each_proposal_once_then_answers(self):
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(mutation=True), AgentDecision(response="Updated."))
        result = self.core(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(result.response, "Updated.")
        self.assertEqual(len(reasoner.contexts), 4)
        self.assertEqual(reasoner.contexts[2].active_goal.objective.summary, "refined objective")
        self.assertEqual(reasoner.contexts[3].active_goal.objective.summary, "refined objective")
        self.assertEqual(self.store.load("goal-a").revision, 3)
        self.assertEqual(len(self.memory.load("preference-1").revisions), 1)

    def test_rejected_goal_proposal_reaches_next_step_without_false_success(self):
        self.store.create(active_goal(), "conversation-1", RETENTION)
        before = self.store.load("goal-a")
        rejected = AgentDecision(goal_proposal=GoalProposal(GoalMutationKind.REQUEST_COMPLETION))
        reasoner = Queued(AgentDecision(goal_id="goal-a"), rejected,
                          AgentDecision(response="More evidence is needed."))
        result = self.core(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "More evidence is needed.")
        self.assertFalse(reasoner.contexts[1].refused_calls)
        self.assertTrue(reasoner.contexts[2].refused_calls)
        self.assertEqual(self.store.load("goal-a"), before)

    def test_a_person_turn_that_uses_its_steps_is_answered_not_silent(self):
        """2026-11-06: the Dual Coil request used its steps and ended without a
        word. The work is kept; one step that can only speak tells him."""
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(),
                          AgentDecision(response="I ran out of steps; the goal is saved."))
        result = self.core(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(result.reason, "budget_exhausted")
        # The line saying she waits is written for her (14 Nov).
        self.assertEqual(result.response, "I've stopped: this request used all my steps. Tell me to carry on "
                         "and I'll continue. I ran out of steps; the goal is saved.")
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual(reasoner.contexts[-1].response_only_reason, "budget_exhausted")
        self.assertEqual(len(self.memory.load("preference-1").revisions), 1)
        self.assertEqual(self.store.load("goal-a").revision, 2)

    def test_a_background_turn_that_uses_its_steps_says_where_it_stands(self):
        """2026-11-10: a background coil job ran out of steps at 14:14 and
        nobody knew until Friedl asked at 14:25."""
        from alx.contracts import CognitionOrigin
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(),
                          AgentDecision(response="The coil runs stopped; the goal is saved."))
        result = self.core(reasoner).process(conversation(), RETENTION, 2,
                                             origin=CognitionOrigin.SELF_REQUESTED)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(result.reason, "budget_exhausted")
        self.assertEqual(reasoner.contexts[-1].response_only_reason, "budget_exhausted")

    def test_a_person_whose_last_step_spends_the_ceiling_is_still_answered(self):
        """Review of f498186: the last regular step used the remaining bill
        allowance, so the step-limit answer was refused by the ceiling."""
        from alx.observability import BudgetExceeded
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(),
                          AgentDecision(response="I ran out of steps; the goal is saved."))
        core = self.core(reasoner)
        checks = []

        def check(task_id):
            checks.append(task_id)
            if len(checks) > 2:  # the two regular steps pass; the ceiling is then spent
                raise BudgetExceeded(task_id, 26, 26)

        core._budget_check = check
        result = core.process(conversation(), RETENTION, 2)
        self.assertEqual(result.state, CoreState.RESPONDED)
        # The line saying she waits is written for her (14 Nov).
        self.assertEqual(result.response, "I've stopped: this request used all my steps. Tell me to carry on "
                         "and I'll continue. I ran out of steps; the goal is saved.")

    def test_a_background_turn_stays_under_the_ceiling_at_its_last_step(self):
        from alx.contracts import CognitionOrigin
        from alx.observability import BudgetExceeded
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        reasoner = Queued(*self.selection(), AgentDecision(response="never asked"))
        core = self.core(reasoner)
        checks = []

        def check(task_id):
            checks.append(task_id)
            if len(checks) > 2:
                raise BudgetExceeded(task_id, 26, 26)

        core._budget_check = check
        result = core.process(conversation(), RETENTION, 2, origin=CognitionOrigin.SELF_REQUESTED)
        self.assertEqual(result.state, CoreState.CHECKPOINTED)
        self.assertEqual(len(reasoner.contexts), 2)

    def test_reselecting_the_same_goal_does_not_buy_an_extra_step(self):
        self.store.create(active_goal(), "conversation-1", RETENTION)
        # Re-selecting the selected goal with nothing new is refused once
        # (D-065: she is told); asking again ends the turn with words.
        reasoner = Queued(*self.selection(), *self.selection(memory=False),
                          *self.selection(memory=False),
                          AgentDecision(response="That one is already open."))
        result = self.core(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(result.reason, "goal_selection_redundant")
        self.assertEqual(len(reasoner.contexts), 5)
        self.assertEqual(len(self.memory.load("preference-1").revisions), 1)

    def test_prior_successful_effect_is_not_redispatched(self):
        self.store.create(active_goal(), "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(goal_proposal=new_goal()),
            AgentDecision(call=removal(), approval_proposal=approval()),
            *self.selection(mutation=True),
            AgentDecision(response="Done."),
        )
        result = self.core(reasoner).process(conversation(), RETENTION, 6)
        self.assertEqual(result.response, "Done.")
        self.assertEqual(len(reasoner.contexts), 6)
        self.assertEqual(self.broker.executed, 1)
        attempts = self.store.load("goal-b").state.attempts
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].result.state.value, "succeeded")
        self.assertEqual(attempts[0].call.call_id, "call-1")

    def test_gateway_and_voice_deliver_the_eventual_answer(self):
        self.store.create(paused_goal(), "conversation-1", RETENTION)
        conversations = SQLiteConversationStore(self.root / "conversation.sqlite3")
        self.addCleanup(conversations.close)
        snapshot = conversations.create("conversation-1", RETENTION)
        for turn in TURNS[:2]:
            snapshot = conversations.append(turn, RETENTION, snapshot.revision)
        reasoner = Queued(*self.selection(), AgentDecision(response="Understood."))
        gateway = ConversationGateway(self.core(reasoner), conversations,
            identifier_factory=lambda: "answer-1", clock=lambda: self.now)
        synthesizer = FakeSynthesizer()
        session = VoiceSession(
            gateway,
            FakeTranscriber((TranscriptionEvent("stt", "transcript-1", TranscriptionState.FINAL,
                                               TURNS[-1].content, self.now),)),
            synthesizer, "friedl", 3, 3650, clock=lambda: self.now,
            identifier_factory=lambda: "turn-3",
        )
        async def collect():
            return [event async for event in session.exchange("conversation-1", incoming_audio())]
        events = asyncio.run(collect())
        self.assertNotIn(VoiceEventKind.ERROR, [event.kind for event in events])
        self.assertEqual([event.text for event in events if event.kind is VoiceEventKind.TEXT], ["Understood."])
        self.assertEqual(synthesizer.responses, ["Understood."])
        answers = [turn for turn in conversations.load("conversation-1").turns
                   if turn.turn_id == "answer-1"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].origin, ConversationOrigin.ALX_RESPONSE)
        self.assertEqual(answers[0].content, "Understood.")
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual(len(self.memory.load("preference-1").revisions), 1)


class RespondedOutcomeTests(unittest.TestCase):
    def test_responded_outcome_requires_nonblank_text(self):
        for response in (None, "", " \n\t", 0, False, [], {}):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, "nonblank response text"):
                CoreOutcome(CoreState.RESPONDED, response=response)
        answer = CoreOutcome(CoreState.RESPONDED, response=" An answer. ")
        self.assertEqual(answer.response, " An answer. ")
        with self.assertRaises(ValueError):
            replace(answer, response=None)

    def test_nonresponse_outcomes_still_need_no_text(self):
        for state in (CoreState.ERROR, CoreState.CHECKPOINTED, CoreState.FINISHED_SILENTLY):
            self.assertIsNone(CoreOutcome(state).response)


if __name__ == "__main__":
    unittest.main()
