"""An active goal that nothing carries forward is offered back to AL/X, a bounded number of times.

The case behind it, 2026-11-03 08:33: she stopped a coding job, said she would
clean up and resume it, and her turn ended with the goal active, five items of
outstanding work and no plan, job or revisit. Nothing woke her until Friedl
asked again.
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
    CapabilityCall, CognitionOrigin, ExecutionPlan, ExecutionStep, GoalState, GoalStatus, GoalStopReason, Objective, PlanStatus,
    SuccessCriterion, WorkItem,
)
from alx.continuity.idle_goal_source import (  # noqa: E402
    IDLE_GOAL_BACKOFF_SECONDS, IDLE_GOAL_SECONDS, MAX_IDLE_GOAL_OFFERS, IdleGoalSource,
)
from alx.continuity.ledger import SQLiteOpportunityLedger  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

START = datetime(2026, 11, 3, 6, 33, tzinfo=UTC)
RETENTION = START + timedelta(days=30)
LATER = timedelta(seconds=IDLE_GOAL_SECONDS + 1)


def plan(status: PlanStatus) -> ExecutionPlan:
    step = ExecutionStep(CapabilityCall("call-coding", "coding", {}, None), (), 0, 0, False)
    return ExecutionPlan("plan", "turn:person-1", "Build the KiCad analysis", None, (step,),
                         status=status)


def goal(goal_id: str = "goal", **changes) -> GoalState:
    state = GoalState(goal_id, Objective("turn:person-1", "Build the KiCad analysis"),
                      (SuccessCriterion("done", "merged"),),
                      outstanding_work=(WorkItem("resume", "resume the coding job"),))
    return replace(state, **changes)


class IdleGoalSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = START
        self.goals = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3",
                                     clock=lambda: self.now)
        self.ledger = SQLiteOpportunityLedger(Path(directory.name) / "ledger.sqlite3")
        self.addCleanup(self.ledger.close)
        self.source = IdleGoalSource(self.goals, self.ledger, enabled=True,
                                     clock=lambda: self.now)

    def later(self, delta: timedelta = LATER) -> None:
        self.now += delta

    def touch(self, goal_id: str = "goal", **changes) -> None:
        snapshot = self.goals.load(goal_id)
        self.goals.replace(replace(snapshot.state, **changes), snapshot.retention_until,
                           snapshot.revision, snapshot.provenance)

    def offer(self):
        (opportunity,) = self.source.due_opportunities()
        self.assertTrue(self.source.claim(opportunity))
        return opportunity

    def test_this_mornings_goal_is_offered_back_with_its_identity(self) -> None:
        self.goals.create(goal(execution_plan=plan(PlanStatus.CANCELLED)), "thread", RETENTION)
        self.assertEqual(self.source.due_opportunities(), ())   # just changed
        self.later()
        opportunity = self.offer()
        self.assertEqual(opportunity.origin, CognitionOrigin.WORK_COMPLETED)
        self.assertEqual(opportunity.conversation_id, "thread")
        self.assertEqual(opportunity.references,
                         ("idle_goal:goal", f"idle_goal_offer:1/{MAX_IDLE_GOAL_OFFERS}"))

    def test_a_turn_that_changes_nothing_is_not_offered_again_for_the_same_state(self) -> None:
        self.goals.create(goal(), "thread", RETENTION)
        self.later()
        self.offer()
        self.later()
        self.assertEqual(self.source.due_opportunities(), ())

    def test_offers_stop_at_the_cap_and_the_last_one_says_so(self) -> None:
        self.goals.create(goal(), "thread", RETENTION)
        references = []
        for _ in range(MAX_IDLE_GOAL_OFFERS):
            self.later()
            references.append(self.offer().references[1])
            # Her turn touched the goal but carried nothing forward.
            self.touch(progress=())
        self.assertEqual(references[-1],
                         f"idle_goal_offer:{MAX_IDLE_GOAL_OFFERS}/{MAX_IDLE_GOAL_OFFERS}")
        self.later()
        self.assertEqual(self.source.due_opportunities(), ())

    def test_goals_with_something_carrying_them_are_never_offered(self) -> None:
        self.goals.create(goal("running", execution_plan=plan(PlanStatus.RUNNING)), "thread", RETENTION)
        self.goals.create(goal("blocked", status=GoalStatus.BLOCKED,
                               stop_reason=GoalStopReason.GENUINELY_BLOCKED,
                               blockers=(WorkItem("b", "waiting on Friedl"),)), "thread", RETENTION)
        self.goals.create(goal("asked", status=GoalStatus.AWAITING_INPUT,
                               stop_reason=GoalStopReason.REQUIRED_INPUT), "thread", RETENTION)
        self.goals.create(goal("nothing_left", outstanding_work=()), "thread", RETENTION)
        self.later()
        self.assertEqual(self.source.due_opportunities(), ())

    def test_an_offer_that_never_ran_is_released_and_offered_again(self) -> None:
        self.goals.create(goal(), "thread", RETENTION)
        self.later()
        opportunity = self.offer()
        self.source.release(opportunity)
        self.assertEqual(self.source.due_opportunities()[0].opportunity_id,
                         opportunity.opportunity_id)

    def test_an_offer_refused_at_a_usage_limit_is_offered_again_after_a_backoff(self) -> None:
        spend = Spend()
        source = IdleGoalSource(self.goals, self.ledger, enabled=True,
                                clock=lambda: self.now, spend=spend)
        self.goals.create(goal(), "thread", RETENTION)
        self.later()
        (first,) = source.due_opportunities()
        source.claim(first)
        spend.refused.add(first.opportunity_id)
        self.assertEqual(source.due_opportunities(), ())          # backing off
        self.later(timedelta(seconds=IDLE_GOAL_BACKOFF_SECONDS + 1))
        (second,) = source.due_opportunities()
        self.assertNotEqual(second.opportunity_id, first.opportunity_id)
        # A refusal is not one of the three: this is still the first offer.
        self.assertEqual(second.references[1], f"idle_goal_offer:1/{MAX_IDLE_GOAL_OFFERS}")
        source.claim(second)
        spend.answered.add(second.opportunity_id)
        self.later(timedelta(hours=1))
        self.assertEqual(source.due_opportunities(), ())          # answered for this state

    def test_off_unless_the_runtime_may_think_unprompted(self) -> None:
        self.goals.create(goal(), "thread", RETENTION)
        self.later()
        source = IdleGoalSource(self.goals, self.ledger, clock=lambda: self.now)
        self.assertEqual(source.due_opportunities(), ())


class Spend:
    def __init__(self) -> None:
        self.answered: set[str] = set()
        self.refused: set[str] = set()

    def dispatch_started(self, opportunity_id: str) -> bool:
        return opportunity_id in self.answered or opportunity_id in self.refused

    def dispatch_answered(self, opportunity_id: str) -> bool:
        return opportunity_id in self.answered


if __name__ == "__main__":
    unittest.main()
