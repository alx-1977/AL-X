"""D-036 goal-mutation steps: closures AL/X already decided run without her.

Once she has decided an exact sequence of goal closures, a plan carries them
and they are applied through the one goal reducer, bound to the goal revision
she decided each against. Mutations at the head of a plan run in the turn that
installed it; any after a capability step run on the background runner. She is
called again only when the sequence is done or something about it needs her,
and her one reply goes to the conversation the plan was installed from.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_execution_plan import (  # noqa: E402
    RETENTION, Ledger, PlanHarness, Reasoner, install, new_goal, plan, resolve, step,
)
from test_model_reasoner import (  # noqa: E402
    FakeModel, ModelReasonerTests,
)

from alx.continuity.plan_source import PlanAttentionSource  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, ConversationOrigin, ConversationTurn, Evidence, ExecutionStep,
    GoalMutationKind, GoalProposal, GoalSnapshot, GoalStatus, PlannedGoalMutation,
    PlanOperation, PlanStatus, PlanUpdate, SuccessCriterion, WorkItem,
)
from alx.conversation import ConversationGateway, SQLiteConversationStore  # noqa: E402
from alx.core import CoreState, ModelReasoner  # noqa: E402

CANCEL = GoalMutationKind.CANCEL
COMPLETE = GoalMutationKind.REQUEST_COMPLETION


def mutation(goal_id: str, kind=CANCEL, *, evidence=None, wake=False) -> ExecutionStep:
    if evidence is None:
        evidence = (() if kind is CANCEL else (Evidence(
            f"ev-{goal_id}", "observation", supports=("done",),
            source_references=("turn:person-1",)),))
    return ExecutionStep(None, wake_core_on_completion=wake, goal_mutation=PlannedGoalMutation(
        f"close-{goal_id}", goal_id, kind, f"{goal_id} is finished", tuple(evidence)))


def silently(workflow, goal_id="goal") -> AgentDecision:
    return AgentDecision(finish_silently=True, goal_id=goal_id,
                         plan_update=PlanUpdate(PlanOperation.INSTALL, workflow))


def finish(text: str, **changes) -> AgentDecision:
    return replace(resolve(PlanOperation.FINISH, text), **changes)


class GoalMutationHarness(PlanHarness):
    def setUp(self):
        super().setUp()
        for goal_id in ("a", "b", "c", "d"):
            self.store.create(new_goal(goal_id), "thread", RETENTION)

    def revisions(self, *goal_ids):
        return [self.store.load(goal_id).revision for goal_id in goal_ids]

    def statuses(self, *goal_ids):
        return [self.state(goal_id).status for goal_id in goal_ids]

    def markers(self, goal_id):
        return [item.record_id for item in self.state(goal_id).decisions
                if item.record_id.startswith("plan-mutation:")]

    def offers(self):
        return PlanAttentionSource(self.store, Ledger(), True,
                                   clock=lambda: self.now).due_opportunities()


class DecidedClosuresTests(GoalMutationHarness):
    """Required 2, 3, 6 and 9: run in the turn, no Core between, one reply."""

    def test_decided_closures_run_in_the_turn_and_she_replies_once(self):
        reasoner = Reasoner(
            install(plan(mutation("a", COMPLETE), mutation("b"), mutation("c", COMPLETE)),
                    "I'm closing a, b and c now."),
            finish("Closed a, b and c. d is unclear, so it stays open."),
        )
        outcome = self.person(self.agent(reasoner))
        # The words written before the work are not delivered; the outcome is.
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Closed a, b and c. d is unclear, so it stays open.")
        self.assertEqual(reasoner.calls, 2)
        # Every mutation was applied before her second call: none between them.
        second = reasoner.contexts[1]
        self.assertEqual(second.active_goal.execution_plan.attention.reason, "plan_steps_done")
        offered = [item.goal_id for item in second.unfinished_goals]
        self.assertTrue({"a", "b", "c"}.isdisjoint(offered))
        self.assertEqual(self.statuses("a", "b", "c"),
                         [GoalStatus.COMPLETED, GoalStatus.CANCELLED, GoalStatus.COMPLETED])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.plan_of().status, PlanStatus.COMPLETED)
        # The ambiguous goal was never touched, and she still sees it.
        self.assertIn("d", offered)
        self.assertEqual(self.revisions("d"), [1])
        # Nothing is left for a later wake.
        self.work(self.agent(Reasoner()))
        self.assertEqual(self.offers(), ())

    def test_a_cleanup_goal_she_creates_can_carry_the_plan(self):
        reasoner = Reasoner(
            AgentDecision(
                finish_silently=True,
                goal_proposal=GoalProposal(
                    GoalMutationKind.CREATE, "Close finished goals",
                    (SuccessCriterion("closed", "clear goals closed"),)),
                plan_update=PlanUpdate(PlanOperation.INSTALL,
                                       plan(mutation("a"), mutation("b"))),
            ),
            finish("Closed a and b.", goal_id="cleanup"),
        )
        agent = self.agent(reasoner, identifier_factory=lambda: "cleanup")
        self.assertEqual(self.person(agent).response, "Closed a and b.")
        self.assertEqual(self.statuses("a", "b"), [GoalStatus.CANCELLED] * 2)
        self.assertEqual(reasoner.calls, 2)

    def test_a_checkpointed_closure_returns_to_her_within_the_turn(self):
        reasoner = Reasoner(
            silently(plan(mutation("a", wake=True), mutation("b"))),
            resolve(PlanOperation.RESUME, "Continuing."),
            finish("Both closed."),
        )
        outcome = self.person(self.agent(reasoner))
        self.assertEqual(reasoner.contexts[1].active_goal.execution_plan.attention.reason,
                         "plan_checkpoint")
        self.assertEqual(outcome.response, "Both closed.")
        self.assertEqual(self.statuses("a", "b"), [GoalStatus.CANCELLED] * 2)

    def test_closures_after_a_capability_step_run_on_the_background_runner(self):
        reasoner = Reasoner(install(plan(step("other"), mutation("a"), mutation("b"))),
                            finish("Ran it and closed a and b."))
        agent = self.agent(reasoner)
        # Work remains to run after the turn, so her acknowledgement stands.
        self.assertEqual(self.person(agent).response, "Started.")
        self.assertEqual(self.statuses("a"), [GoalStatus.ACTIVE])
        self.work(agent)
        self.assertEqual(self.names(), ["other"])
        self.assertEqual(self.statuses("a", "b"), [GoalStatus.CANCELLED] * 2)
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.occasion(agent).response, "Ran it and closed a and b.")
        self.assertEqual(reasoner.calls, 2)


class ConversationRoutingTests(GoalMutationHarness):
    """Required 1: the reply returns to the conversation that asked."""

    def test_a_plan_hosted_by_an_older_goal_answers_the_conversation_that_asked(self):
        conversations = SQLiteConversationStore(Path(self.path).with_name("turns.sqlite3"))
        self.addCleanup(conversations.close)
        reasoner = Reasoner(install(plan(step("other"), mutation("a"), mutation("b"))),
                            finish("Closed a and b."))
        agent = self.agent(reasoner)
        gateway = ConversationGateway(agent, conversations, clock=lambda: self.now)
        gateway.receive_conversation_turn(ConversationTurn(
            "elsewhere", "person-1", ConversationOrigin.TYPED, "Tidy up, please.",
            self.now, "friedl"), 4, RETENTION)
        # Hosted by "goal", whose own conversation is "thread".
        self.assertEqual(self.store.load("goal").conversation_id, "thread")
        self.work(agent)
        (offer,) = self.offers()
        self.assertEqual(offer.conversation_id, "elsewhere")
        outcome = gateway.receive_cognition_opportunity(
            offer.conversation_id, offer, 4, RETENTION)
        self.assertEqual(outcome.response, "Closed a and b.")
        replies = [item.content for item in conversations.load("elsewhere").turns
                   if item.origin is ConversationOrigin.ALX_RESPONSE]
        self.assertEqual(replies, ["Started.", "Closed a and b."])
        try:
            hosting = conversations.load("thread").turns
        except Exception:  # noqa: BLE001 - never created is the expected case
            hosting = ()
        self.assertEqual([item for item in hosting
                          if item.origin is ConversationOrigin.ALX_RESPONSE], [])

    def test_a_plan_recorded_without_its_conversation_answers_in_its_goals(self):
        snapshot = self.store.load("goal")
        legacy = replace(plan(step("other"), objective_source="turn:person-1",
                              objective_summary="Do the work"))
        self.assertIsNone(legacy.source_conversation_id)
        self.assertEqual(GoalSnapshot(replace(snapshot.state, execution_plan=legacy), "thread",
                                      1, RETENTION).plan_conversation_id, "thread")
        bound = replace(legacy, source_conversation_id="elsewhere")
        self.assertEqual(GoalSnapshot(replace(snapshot.state, execution_plan=bound), "thread",
                                      1, RETENTION).plan_conversation_id, "elsewhere")


class RefusalLoopTests(GoalMutationHarness):
    """Required 4 and 5: an unchanged refusal neither loops nor silences her."""

    def completing(self, text):
        return finish(text, response_requires_goal_commit=True,
                      goal_proposal=GoalProposal(COMPLETE))

    def test_a_refused_host_completion_still_delivers_one_reply_and_stops(self):
        reasoner = Reasoner(
            silently(plan(mutation("a"), mutation("b"))),
            self.completing("Done, and the cleanup is complete."),
            self.completing("Done, and the cleanup is complete."),
            AgentDecision(response="a and b are closed. The cleanup itself stays open."),
        )
        outcome = self.person(self.agent(reasoner), budget=6)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "a and b are closed. The cleanup itself stays open.")
        # Told once, reconsidered once, then one step that can only speak.
        self.assertEqual(reasoner.calls, 4)
        self.assertEqual(reasoner.contexts[2].refused_calls[0]["reason"],
                         "completion_lacks_sourced_evidence")
        self.assertEqual(reasoner.contexts[3].response_only_reason, "goal_proposal_invalid")
        # The evidence requirement held, and nothing will offer it again.
        self.assertEqual(self.statuses("goal", "a", "b"),
                         [GoalStatus.ACTIVE, GoalStatus.CANCELLED, GoalStatus.CANCELLED])
        self.assertTrue(self.plan_of().attention.blocked)
        self.assertEqual(self.offers(), ())

    def test_a_plan_wake_refused_the_same_way_is_not_offered_again(self):
        reasoner = Reasoner(
            install(plan(step("other"), mutation("a"))),
            self.completing("All done."), self.completing("All done."),
            AgentDecision(response="a is closed; the cleanup goal stays open."),
        )
        agent = self.agent(reasoner)
        self.person(agent)
        self.work(agent)
        self.assertEqual(len(self.offers()), 1)
        outcome = self.occasion(agent, budget=6)
        self.assertEqual(outcome.response, "a is closed; the cleanup goal stays open.")
        self.assertEqual(reasoner.calls, 4)
        self.assertEqual(self.offers(), ())
        self.later(86_400)
        self.assertEqual(self.offers(), ())


class ExactlyOnceTests(GoalMutationHarness):
    """Required behaviour 4 and 5 of the mechanism: once, and resumed."""

    def test_each_mutation_is_written_once(self):
        reasoner = Reasoner(silently(plan(mutation("a"), mutation("b", COMPLETE))),
                            finish("Closed."))
        agent = self.agent(reasoner)
        self.person(agent)
        self.work(agent)
        agent.advance_due_plans()
        self.assertEqual(self.revisions("a", "b"), [2, 2])
        self.assertEqual([len(self.markers("a")), len(self.markers("b"))], [1, 1])

    def test_restart_resumes_the_remaining_sequence_without_repeating_one(self):
        agent = self.installed(step("other"), mutation("a"), mutation("b"), mutation("c"))
        written = agent._write_plan
        stopped = []

        def stop_after_a(snapshot, workflow):
            if workflow.cursor == 2 and not stopped:
                stopped.append(True)
                raise RuntimeError("process stopped")
            return written(snapshot, workflow)

        # The process stops after a's write and before the cursor moves.
        agent._write_plan = stop_after_a
        with self.assertRaises(RuntimeError):
            self.work(agent)
        self.assertEqual(self.revisions("a", "b", "c"), [2, 1, 1])
        self.assertEqual(self.plan_of().cursor, 1)

        self.restart()
        fresh = Reasoner()
        self.work(self.agent(fresh))
        self.assertEqual(self.revisions("a", "b", "c"), [2, 2, 2])
        self.assertEqual(self.statuses("a", "b", "c"), [GoalStatus.CANCELLED] * 3)
        self.assertEqual(self.plan_of().attention.reason, "plan_steps_done")
        self.assertEqual(fresh.calls, 0)

    def test_restart_between_steps_resumes_from_the_durable_cursor(self):
        agent = self.installed(step("other"), mutation("a", wake=True),
                               mutation("b"), mutation("c"))
        self.work(agent)
        self.assertEqual(self.plan_of().attention.reason, "plan_checkpoint")
        self.restart()
        resumed = Reasoner(resolve(PlanOperation.RESUME), finish("All three closed."))
        self.assertEqual(self.occasion(self.agent(resumed)).response, "All three closed.")
        self.assertEqual(self.statuses("a", "b", "c"), [GoalStatus.CANCELLED] * 3)
        self.assertEqual(self.revisions("a", "b", "c"), [2, 2, 2])


class ReturnsToCoreTests(GoalMutationHarness):
    """Anything changed is hers again, and the goal is left untouched."""

    def assert_returned(self, reason, *, applied=("a",), untouched=("c",)):
        current = self.plan_of()
        self.assertEqual((current.status, current.attention.reason),
                         (PlanStatus.NEEDS_CORE, reason))
        for goal_id in applied:
            self.assertEqual(self.revisions(goal_id), [2])
        for goal_id in untouched:
            self.assertEqual(self.revisions(goal_id), [1])

    def test_a_changed_goal_revision_returns_to_her(self):
        reasoner = Reasoner(install(plan(step("other"), mutation("a"), mutation("b"),
                                         mutation("c"))),
                            resolve(PlanOperation.CANCEL, "b changed, so I left it open."))
        agent = self.agent(reasoner)
        self.person(agent)
        self.set_goal("b", outstanding_work=(WorkItem("new", "Fresh work"),))
        self.work(agent)
        self.assert_returned("plan_goal_revision_changed")
        self.assertEqual(self.statuses("b"), [GoalStatus.ACTIVE])
        self.assertEqual(self.revisions("b"), [2])
        self.assertEqual(self.markers("b"), [])
        self.occasion(agent)
        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(reasoner.contexts[1].active_goal.execution_plan.attention.reason,
                         "plan_goal_revision_changed")

    def test_changed_evidence_on_the_goal_returns_to_her(self):
        agent = self.installed(step("other"), mutation("a"), mutation("b"), mutation("c"))
        self.set_goal("b", evidence=(Evidence("late", "observation", source_references=(
            "turn:person-1",)),))
        self.work(agent)
        self.assert_returned("plan_goal_revision_changed")
        self.assertEqual(self.statuses("b"), [GoalStatus.ACTIVE])

    def test_changed_evidence_the_plan_relied_on_returns_to_her(self):
        self.set_goal(context={"reviewed": "goal list v1"})
        agent = self.installed(step("other"), mutation("a"), mutation("b"),
                               context_preconditions={"reviewed": "goal list v1"})
        self.set_goal(context={"reviewed": "goal list v2"})
        self.work(agent)
        self.assert_returned("plan_precondition_changed", applied=(), untouched=("a", "b"))

    def test_a_refused_mutation_returns_to_her_and_writes_nothing(self):
        # Installed as durable state directly: the runner never relies on the
        # install-time check having happened.
        steps = tuple(replace(item, goal_mutation=replace(item.goal_mutation,
                                                          expected_revision=1))
                      for item in (mutation("a"), mutation("b", COMPLETE, evidence=()),
                                   mutation("c")))
        self.set_goal(execution_plan=plan(*steps, objective_source="turn:person-1",
                                          objective_summary="Do the work"))
        self.work(self.agent())
        self.assert_returned("plan_goal_mutation_refused")
        self.assertEqual(self.plan_of().attention.facts,
                         ("plan_goal_mutation_refused", "completion_lacks_sourced_evidence"))
        self.assertEqual(self.revisions("b"), [1])

    def test_a_failed_write_returns_to_her_and_is_not_retried(self):
        agent = self.installed(step("other"), mutation("a"), mutation("b"), mutation("c"))
        replace_goal = self.store.replace

        def refuse_b(state, *arguments):
            if state.goal_id == "b":
                raise OSError("disk unavailable")
            return replace_goal(state, *arguments)

        self.store.replace = refuse_b
        self.work(agent)
        self.work(agent)
        self.assert_returned("plan_goal_mutation_failed")
        self.assertEqual(self.revisions("b"), [1])

    def test_a_change_within_the_turn_stops_there_and_she_answers_once(self):
        reasoner = Reasoner(silently(plan(mutation("a"), mutation("b"))),
                            finish("Closed a; b had changed."))
        agent = self.agent(reasoner)
        apply = agent._apply_plan_update

        def then_b_changes(*arguments, **options):
            # b moves on after the plan bound its revision, before it runs.
            installed = apply(*arguments, **options)
            self.set_goal("b", outstanding_work=(WorkItem("new", "Fresh work"),))
            return installed

        agent._apply_plan_update = then_b_changes
        outcome = self.person(agent)
        self.assertEqual(outcome.response, "Closed a; b had changed.")
        self.assertEqual(reasoner.contexts[1].active_goal.execution_plan.attention.reason,
                         "plan_goal_revision_changed")

    def test_a_mutation_refused_at_install_is_never_installed(self):
        reasoner = Reasoner(silently(plan(mutation("a"), mutation("b", COMPLETE, evidence=()))),
                            AgentDecision(response="b has no evidence yet.", goal_id="goal"))
        self.person(self.agent(reasoner))
        refused = reasoner.contexts[1].refused_calls[0]["reason"]
        self.assertEqual(refused,
                         "plan_goal_mutation_refused:b:completion_lacks_sourced_evidence")
        self.assertIsNone(self.plan_of())
        self.assertEqual(self.revisions("a", "b"), [1, 1])

    def test_install_refuses_its_own_goal_and_goals_she_was_not_offered(self):
        for target, reason in (("goal", "plan_mutation_targets_own_goal"),
                               ("ghost", "plan_goal_not_offered")):
            with self.subTest(target=target):
                reasoner = Reasoner(silently(plan(mutation(target))),
                                    AgentDecision(response="Not possible.", goal_id="goal"))
                self.person(self.agent(reasoner))
                self.assertEqual(reasoner.contexts[1].refused_calls[0]["reason"], reason)
                self.assertIsNone(self.plan_of())


class OrdinaryBehaviourTests(GoalMutationHarness):
    """Required 7: single-goal updates and capability plans are as they were."""

    def test_an_ordinary_single_goal_closure_is_unchanged(self):
        reasoner = Reasoner(AgentDecision(goal_id="a", response="Closed a.",
                                          goal_proposal=GoalProposal(CANCEL)))
        outcome = self.person(self.agent(reasoner))
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.statuses("a"), [GoalStatus.CANCELLED])
        self.assertEqual(self.revisions("a"), [2])
        self.assertEqual(self.state("a").decisions, ())
        self.assertIsNone(self.plan_of())

    def test_a_coding_plan_still_acknowledges_at_install_and_runs_in_the_background(self):
        reasoner = Reasoner(install(plan(step("coding"), step("merge"))))
        outcome = self.person(self.agent(reasoner))
        self.assertEqual(outcome.response, "Started.")
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.plan_of().status, self.plan_of().cursor), (PlanStatus.RUNNING, 0))

    def test_the_planned_path_uses_the_one_goal_reducer(self):
        import inspect

        from alx.core.loop import CoreAgent
        source = inspect.getsource(CoreAgent._write_planned_mutation)
        self.assertIn("self._reduce_goal_mutation(", source)
        for status in ("GoalStatus.CANCELLED", "GoalStatus.COMPLETED", "_derive_goal_status"):
            self.assertNotIn(status, source)
        self.assertIn("self._reduce_goal_mutation(",
                      inspect.getsource(CoreAgent._reduce_goal_proposal))

    def test_a_plan_cannot_close_its_own_goal_or_one_goal_twice(self):
        with self.assertRaises(ValueError):
            plan(mutation("a"), replace(mutation("a"), goal_mutation=replace(
                mutation("a").goal_mutation, step_id="again")))
        with self.assertRaises(ValueError):
            self.set_goal(execution_plan=plan(
                replace(mutation("goal"), goal_mutation=replace(
                    mutation("goal").goal_mutation, expected_revision=1)),
                objective_source="turn:person-1", objective_summary="Do the work"))
        with self.assertRaises(ValueError):
            PlannedGoalMutation("s", "a", GoalMutationKind.UPDATE, "reason")


class ReasonerParsingTests(unittest.TestCase):
    context = ModelReasonerTests.context
    plan_output = ModelReasonerTests.plan_output
    plan_step_schema = ModelReasonerTests.plan_step_schema

    def test_a_goal_mutation_step_is_parsed_without_a_revision(self):
        output = self.plan_output({
            "step_id": "close-a", "goal_id": "a", "operation": "cancel",
            "reason": "merged", "new_evidence": [], "wake_core_on_completion": False,
        }, disposition="finish_silently")
        decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(self.context())
        planned = decision.plan_update.plan.steps[0]
        self.assertIsNone(planned.call)
        self.assertEqual((planned.goal_mutation.goal_id, planned.goal_mutation.kind),
                         ("a", CANCEL))
        # Bound by the Core when it installs the plan, never by the model.
        self.assertIsNone(planned.goal_mutation.expected_revision)
        self.assertIsNone(decision.plan_update.plan.source_conversation_id)

    def test_the_schema_offers_only_closing_mutations(self):
        shapes = self.plan_step_schema()["anyOf"]
        mutation_shape = next(item for item in shapes if "goal_id" in item["properties"])
        self.assertEqual(mutation_shape["properties"]["operation"]["enum"],
                         ["cancel", "request_completion"])


del ModelReasonerTests

if __name__ == "__main__":
    unittest.main()
