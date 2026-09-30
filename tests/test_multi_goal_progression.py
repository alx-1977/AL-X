"""One Core turn can inspect distinct goals without cycling through them."""
import asyncio
import inspect
from dataclasses import replace

from test_goal_context_selection import (
    Fixture, Queued, active_goal, conversation, RETENTION, NOW,
)
from alx.contracts import (
    AgentDecision, CapabilityCall, ContentOrigin, Evidence, GoalMutationKind,
    GoalProposal, GoalStatus, MemoryKind, MemoryProposal, RetentionPolicy, WorkItem,
)
from alx.core import CoreState
from alx.core.loop import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.interfaces.live_voice import VoiceEvent, VoiceEventKind
from alx.interfaces.server import LiveVoiceServer


def cancel(goal_id):
    return AgentDecision(goal_id=goal_id, goal_proposal=GoalProposal(GoalMutationKind.CANCEL))


class MultiGoalProgressionTests(Fixture):
    def create(self, *identifiers):
        for identifier in identifiers:
            self.store.create(active_goal(identifier), "conversation-1", RETENTION)

    def test_three_existing_goals_reconcile_and_survive_restart(self):
        self.create("a", "b", "c")
        reasoner = Queued(
            AgentDecision(goal_id="a"), cancel("a"), cancel("b"), cancel("c"),
            AgentDecision(response="Those three are obsolete and closed."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 5)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(len(reasoner.contexts), 5)
        self.assertEqual(self.broker.executed, 0)
        with SQLiteGoalStore(self.root / "goals.sqlite3") as restarted:
            for identifier in ("a", "b", "c"):
                self.assertEqual(restarted.load(identifier).state.status, GoalStatus.CANCELLED)

    def test_reproduced_effect_then_second_selection_is_allowed(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a"),
            AgentDecision(call=CapabilityCall("work-a", "study_question", {})),
            cancel("b"),
            AgentDecision(response="The action succeeded and the obsolete goal is closed."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(self.broker.executed, 1)
        self.assertEqual(len(self.store.load("a").state.attempts), 1)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.CANCELLED)

    def test_departed_goal_cannot_be_revisited_even_after_completion(self):
        self.create("a", "b")
        reasoner = Queued(cancel("a"), cancel("b"), AgentDecision(goal_id="a"))
        result = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(result.state, CoreState.CHECKPOINTED)
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual(self.store.load("a").revision, 2)

    def test_active_goal_cycle_preserves_answer_but_does_not_dispatch_again(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a", call=CapabilityCall("a-work", "study_question", {})),
            AgentDecision(goal_id="b", call=CapabilityCall("b-work", "study_question", {})),
            AgentDecision(goal_id="a", response="Both actions ran; further work remains.",
                          goal_proposal=GoalProposal(GoalMutationKind.CANCEL)),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(result.response, "Both actions ran; further work remains.")
        self.assertEqual(self.broker.executed, 2)
        self.assertEqual(self.store.load("a").state.status, GoalStatus.ACTIVE)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.ACTIVE)

    def test_inspection_allows_a_second_offered_goal_without_a_write(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="b"),
            AgentDecision(goal_id="b", response="I inspected both goals."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "I inspected both goals.")
        self.assertEqual(result.reason, None)
        self.assertEqual(reasoner.contexts[1].active_goal.goal_id, "a")
        self.assertEqual(reasoner.contexts[2].active_goal.goal_id, "b")
        self.assertEqual(self.store.load("a").revision, 1)
        self.assertEqual(self.store.load("b").revision, 1)

    def test_three_distinct_inspections_fit_within_the_existing_step_budget(self):
        self.create("a", "b", "c")
        reasoner = Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="b"),
            AgentDecision(goal_id="c"), AgentDecision(response="Inspected."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 4)
        self.assertEqual(result.response, "Inspected.")
        self.assertEqual(
            [context.active_goal.goal_id if context.active_goal else None
             for context in reasoner.contexts],
            [None, "a", "b", "c"],
        )
        self.assertEqual([self.store.load(g).revision for g in ("a", "b", "c")], [1, 1, 1])

    def test_inspection_only_cycle_is_refused_before_loading_the_old_goal(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="b"),
            AgentDecision(goal_id="a"),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(result.state, CoreState.CHECKPOINTED)
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(len(reasoner.contexts), 3)
        self.assertEqual([self.store.load(g).revision for g in ("a", "b")], [1, 1])

    def test_inspection_does_not_authorize_an_unoffered_goal(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="not-offered"),
            AgentDecision(goal_id="b", response="I can inspect the offered goal."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "I can inspect the offered goal.")
        self.assertEqual(reasoner.contexts[2].active_goal.goal_id, "a")
        self.assertEqual(reasoner.contexts[2].refused_goal_selections[0]["reason"],
                         "goal_selection_unknown")
        self.assertEqual([self.store.load(g).revision for g in ("a", "b")], [1, 1])

    def test_noop_mutation_cannot_manufacture_progress_with_a_revision(self):
        self.create("a", "b")
        reasoner = Queued(
            AgentDecision(goal_id="a", goal_proposal=GoalProposal(GoalMutationKind.UPDATE)),
            cancel("b"),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(result.reason, "goal_selection_no_progress")
        self.assertEqual(len(reasoner.contexts), 1)
        self.assertEqual(self.store.load("b").revision, 1)

    def test_independent_answer_survives_deferred_revisit_without_mutation(self):
        self.create("a")
        self.store.create(replace(active_goal("b"), outstanding_work=(WorkItem("pending", "Still needed"),)),
                          "conversation-1", RETENTION)
        reasoner = Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="b"),
            AgentDecision(goal_id="a", goal_proposal=GoalProposal(GoalMutationKind.CANCEL),
                          response="The remaining work is still open."),
        )
        result = self.agent(reasoner).process(conversation(), RETENTION, 25)
        self.assertEqual(result.response, "The remaining work is still open.")
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(self.store.load("a").revision, 1)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.AWAITING_INPUT)
        self.assertEqual(result.snapshot.state.status, GoalStatus.AWAITING_INPUT)
        # A fresh turn may resume that durable work; the guard is not goal state.
        following = self.agent(Queued(cancel("a"), AgentDecision(response="Closed.")))
        self.assertEqual(following.process(conversation(), RETENTION, 2).response, "Closed.")

    def test_deferred_answer_parks_a_blocked_departing_goal(self):
        self.create("a")
        self.store.create(replace(active_goal("b"), blockers=(WorkItem("blocked", "Needs evidence"),)),
                          "conversation-1", RETENTION)
        result = self.agent(Queued(
            AgentDecision(goal_id="a"), AgentDecision(goal_id="b"),
            AgentDecision(goal_id="a", response="The evidence is still missing."),
        )).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "The evidence is still missing.")
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(result.snapshot.state.status, GoalStatus.BLOCKED)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.BLOCKED)
        self.assertEqual(self.store.load("a").revision, 1)

    def test_commit_dependent_answer_does_not_survive_refused_selection(self):
        self.create("a", "b")
        reasoner = Queued(AgentDecision(goal_id="a"), AgentDecision(goal_id="b"), AgentDecision(
            goal_id="a", goal_proposal=GoalProposal(GoalMutationKind.CANCEL),
            response="I closed it.", response_requires_goal_commit=True,
        ))
        result = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertIsNone(result.response)
        self.assertEqual(result.state, CoreState.CHECKPOINTED)
        self.assertEqual(result.reason, "goal_selection_revisited")
        self.assertEqual(self.store.load("a").revision, 1)

    def test_preserved_answer_still_passes_memory_grounding(self):
        self.create("a", "b")
        invalid = MemoryProposal("m", MemoryKind.FACTUAL, "unsupported", ("turn:missing",), NOW)
        reasoner = Queued(AgentDecision(goal_id="a"), AgentDecision(
            goal_id="b", response="An independent answer.", memory_proposals=(invalid,),
        ), AgentDecision(response="Corrected."))
        result = self.agent(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(result.response, "Corrected.")
        self.assertEqual(reasoner.contexts[-1].refused_calls[0]["reason"], "memory_proposal_invalid")

    def test_existing_step_budget_bounds_productive_progress(self):
        self.create("a", "b", "c")
        result = self.agent(Queued(cancel("a"), cancel("b"), cancel("c"))).process(
            conversation(), RETENTION, 2)
        self.assertEqual(result.reason, "budget_exhausted")
        self.assertEqual(self.store.load("a").state.status, GoalStatus.CANCELLED)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.CANCELLED)
        self.assertEqual(self.store.load("c").revision, 1)

    def test_single_goal_inspection_and_answer_is_unchanged(self):
        self.create("a")
        reasoner = Queued(AgentDecision(goal_id="a"), AgentDecision(goal_id="a", response="Still open."))
        result = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(result.response, "Still open.")
        self.assertIsNone(result.reason)
        self.assertEqual(self.store.load("a").revision, 1)

    def test_completion_still_requires_sourced_evidence_after_switch(self):
        self.create("a", "b")
        reasoner = Queued(AgentDecision(goal_id="a"), AgentDecision(
            goal_id="b", goal_proposal=GoalProposal(GoalMutationKind.REQUEST_COMPLETION),
            response="The second goal still needs evidence.",
        ))
        core = self.agent(reasoner)
        result = core.process(conversation(), RETENTION, 2)
        self.assertEqual(result.reason, "goal_proposal_rejected")
        self.assertEqual(core.last_goal_rejection["reason"], "completion_lacks_sourced_evidence")
        self.assertEqual(self.store.load("a").revision, 1)
        self.assertEqual(self.store.load("b").revision, 1)

    def test_grounded_completion_after_switch_still_succeeds(self):
        self.create("a", "b")
        evidence = Evidence("ev", "confirmation", supports=("a-1",), source_references=("turn:turn-3",))
        reasoner = Queued(AgentDecision(goal_id="a"), AgentDecision(
            goal_id="b", goal_proposal=GoalProposal(GoalMutationKind.REQUEST_COMPLETION, new_evidence=(evidence,)),
            response="The second goal is complete.", response_requires_goal_commit=True,
        ))
        result = self.agent(reasoner).process(conversation(), RETENTION, 2)
        self.assertEqual(result.state, CoreState.RESPONDED)
        self.assertEqual(self.store.load("a").revision, 1)
        self.assertEqual(self.store.load("b").state.status, GoalStatus.COMPLETED)

    def test_departed_goal_provenance_survives_transitions(self):
        external = RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, NOW)
        self.store.create(active_goal("a"), "conversation-1", RETENTION, external)
        self.create("b", "c")
        result = self.agent(Queued(cancel("a"), cancel("b"), cancel("c"),
                                  AgentDecision(response="Reconciled."))).process(conversation(), RETENTION, 4)
        self.assertIn(ContentOrigin.EXTERNAL, result.response_provenance.origins)
        self.assertIn(ContentOrigin.EXTERNAL, self.store.load("c").provenance.origins)

    def test_superseded_count_guard_is_deleted(self):
        self.assertNotIn("_MAX_GOAL_SELECTIONS", inspect.getsource(CoreAgent))
        self.assertNotIn("goal_selection_exhausted", inspect.getsource(CoreAgent))

    def test_voice_keeps_consuming_audio_after_selection_checkpoint(self):
        for reason in ("goal_selection_no_progress", "goal_selection_revisited", "goal_selection_redundant"):
            with self.subTest(reason=reason):
                consumed, sent = [], []
                class Session:
                    async def exchange(self, conversation_id, audio, deliveries=None, typed=None, **kwargs):
                        async for frame in audio:
                            consumed.append(frame)
                            if frame == 1:
                                yield VoiceEvent(
                                    VoiceEventKind.DIAGNOSTIC,
                                    diagnostic={"code": "core.checkpointed", "reason": reason},
                                )
                            yield VoiceEvent(VoiceEventKind.LISTENING)
                class Connection:
                    async def send(self, payload):
                        sent.append(payload)
                async def audio():
                    yield 1
                    yield 2
                server = LiveVoiceServer.__new__(LiveVoiceServer)
                server._session = Session()
                server._delivery_queues = {}
                server._await_audio_confirmation = False
                server._audio = lambda *_: audio()
                asyncio.run(server._exchange_once(Connection(), "conversation-1"))
                self.assertEqual(consumed, [1, 2])
                self.assertTrue(any(reason in item for item in sent))
