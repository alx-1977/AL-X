"""One go for the writes she listed when she asked for it (2026-11-13).

On 13 Nov one write-back took three goes from Friedl: his first was spent on
the library write-back she had shown beside it, and his second arrived while
she was working in a background round, so it was not his latest turn there.
Now a reply that asks for his go lists the exact calls. His next message
approves each of them once, beside each other and in a background round.
Anything she did not list keeps the old rules.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.contracts import (  # noqa: E402
    AgentDecision, ApprovalLifecycle, ApprovalScope, ConversationOrigin, ConversationSnapshot, ConversationTurn,
)
from alx.goals import SQLiteGoalStore  # noqa: E402
import test_one_turn_one_review as harness  # noqa: E402
from test_one_turn_one_review import (  # noqa: E402
    NOW, RETENTION, REQUEST_REVIEW_BEYOND_ALLOWANCE, Queued, _turn, approved, review_call,
)


def later(turn_id, origin, content, person, minutes):
    """A turn spoken some minutes after she asked."""
    return ConversationTurn("conversation-1", turn_id, origin, content, NOW + timedelta(minutes=minutes), person)


def scope(call):
    return ApprovalScope(call.capability_id, call.arguments)


def conversation(*turns):
    return ConversationSnapshot("conversation-1", tuple(turns), len(turns), RETENTION)


ASK = _turn("turn-2", ConversationOrigin.TYPED, "Fix PRs 21 and 22.", "friedl")
HER_ASK = _turn("turn-3", ConversationOrigin.ALX_RESPONSE, "Both ready. Say go to request both reviews.", None)
GO = later("turn-4", ConversationOrigin.TYPED, "go", "friedl", 1)
LATER = later("turn-5", ConversationOrigin.ALX_RESPONSE, "Working on it.", None, 2)


class GoRequestTests(unittest.TestCase):
    """Uses the one-review-per-instruction harness, whose rules still hold."""

    setUp = harness.OneTurnOneReviewTests.setUp
    agent = harness.OneTurnOneReviewTests.agent
    goal = staticmethod(harness.OneTurnOneReviewTests.goal)

    def ask_for_go(self, *calls):
        first = conversation(_turn("turn-1", ConversationOrigin.ALX_RESPONSE, "Ready.", None), ASK)
        self.agent(Queued(
            AgentDecision(goal_proposal=self.goal()),
            AgentDecision(response="Both ready. Say go.", go_requests=tuple(scope(c) for c in calls)),
        )).process(first, RETENTION, 4)
        state = self.store.load("goal-1").state
        self.assertEqual([item.lifecycle for item in state.approvals], [ApprovalLifecycle.REQUESTED] * len(calls))

    def test_one_go_covers_every_write_she_listed(self) -> None:
        a, b = review_call("call-a", "approve-a", 21), review_call("call-b", "approve-b", 22)
        self.ask_for_go(a, b)
        self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(call=b, approval_proposal=approved(b, "turn:turn-4")),
            AgentDecision(response="Both requested."),
        )).process(conversation(ASK, HER_ASK, GO), RETENTION, 6)
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE, REQUEST_REVIEW_BEYOND_ALLOWANCE])
        lifecycles = {item.approval_id.split("|")[0]: item.lifecycle
                      for item in self.store.load("goal-1").state.approvals if item.approval_id.startswith("request")}
        self.assertEqual(set(lifecycles.values()), {ApprovalLifecycle.CONSUMED})

    def test_his_go_counts_in_a_background_round(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        # Her own words came after his go; it is still his latest message.
        self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(response="Requested."),
        )).process(conversation(ASK, HER_ASK, GO, LATER), RETENTION, 6)
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE])

    def test_a_write_she_did_not_list_keeps_the_old_rules(self) -> None:
        a, c = review_call("call-a", "approve-a", 21), review_call("call-c", "approve-c", 99)
        self.ask_for_go(a)
        outcome = self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(call=c, approval_proposal=approved(c, "turn:turn-4")),
            AgentDecision(response="One requested."),
        )).process(conversation(ASK, HER_ASK, GO), RETENTION, 6)
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE])
        reasons = [item.reason_code for item in outcome.snapshot.state.attempts]
        self.assertIn("approval_capability_already_dispatched", reasons)

    def test_each_listed_write_once_and_only_on_his_next_message(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(response="Requested."),
        )).process(conversation(ASK, HER_ASK, GO), RETENTION, 6)
        # The same write again on a later message of his: not listed any more.
        again = review_call("call-a2", "approve-a2", 21)
        newer = later("turn-6", ConversationOrigin.TYPED, "and the other thing", "friedl", 3)
        outcome = self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=again, approval_proposal=approved(again, "turn:turn-4")),
            AgentDecision(response="Blocked."),
        )).process(conversation(ASK, HER_ASK, GO, LATER, newer), RETENTION, 6)
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE])
        self.assertIn("approval_source_not_latest_person_turn",
                      [item.reason_code for item in outcome.snapshot.state.attempts])

    def test_a_request_is_not_approved_by_a_later_unrelated_message(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        other = later("turn-4", ConversationOrigin.TYPED, "what is the weather", "friedl", 1)
        reply = later("turn-5", ConversationOrigin.ALX_RESPONSE, "Sunny.", None, 2)
        later_go = later("turn-6", ConversationOrigin.TYPED, "go", "friedl", 3)
        outcome = self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-6")),
            AgentDecision(response="Requested."),
        )).process(conversation(ASK, HER_ASK, other, reply, later_go), RETENTION, 6)
        # turn-6 is his latest, so the old rule allows exactly this one write,
        # but only because it is the latest turn, not through the request.
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE])
        state = outcome.snapshot.state
        self.assertNotIn(ApprovalLifecycle.CONSUMED, [item.lifecycle for item in state.approvals
                                                      if item.approval_id.startswith("request|")])


    def test_a_reply_without_the_list_ends_the_request(self) -> None:
        """Review of D-080: she says "hold on" and lists nothing; his "ok" approves nothing."""
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        hold = conversation(ASK, HER_ASK, later("turn-4", ConversationOrigin.TYPED, "how is it going", "friedl", 1))
        self.agent(Queued(AgentDecision(goal_id="goal-1"), AgentDecision(response="Found a problem, hold on."))
                   ).process(hold, RETENTION, 4)
        lifecycles = [item.lifecycle for item in self.store.load("goal-1").state.approvals]
        self.assertEqual(lifecycles, [ApprovalLifecycle.WITHDRAWN])

    def test_a_go_spoken_before_she_asked_is_not_an_answer(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        early = _turn("turn-4", ConversationOrigin.TYPED, "go", "friedl")   # same moment as her ask
        outcome = self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(response="Done."),
        )).process(conversation(ASK, HER_ASK, early, LATER), RETENTION, 6)
        self.assertEqual(self.dispatched, [])
        self.assertIn("approval_source_not_latest_person_turn",
                      [item.reason_code for item in outcome.snapshot.state.attempts])

    def test_the_same_call_listed_twice_runs_once(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        twice = review_call("call-a2", "approve-a2", 21)
        first = conversation(_turn("turn-1", ConversationOrigin.ALX_RESPONSE, "Ready.", None), ASK)
        self.agent(Queued(AgentDecision(goal_proposal=self.goal()),
                          AgentDecision(response="Say go.", go_requests=(scope(a), scope(a))))
                   ).process(first, RETENTION, 4)
        self.agent(Queued(
            AgentDecision(goal_id="goal-1"),
            AgentDecision(call=a, approval_proposal=approved(a, "turn:turn-4")),
            AgentDecision(call=twice, approval_proposal=approved(twice, "turn:turn-4")),
            AgentDecision(response="Done."),
        )).process(conversation(ASK, HER_ASK, GO), RETENTION, 6)
        self.assertEqual(self.dispatched, [REQUEST_REVIEW_BEYOND_ALLOWANCE])

    def test_a_request_from_another_conversation_approves_nothing(self) -> None:
        a = review_call("call-a", "approve-a", 21)
        self.ask_for_go(a)
        state = self.store.load("goal-1").state
        [request] = [item for item in state.approvals if item.lifecycle is ApprovalLifecycle.REQUESTED]
        from alx.core.loop import CoreAgent
        from dataclasses import replace as moved
        elsewhere = ConversationSnapshot("conversation-2", tuple(
            moved(item, conversation_id="conversation-2") for item in (ASK, HER_ASK, GO)), 3, RETENTION)
        proposal = approved(a, "turn:turn-4")
        self.assertIsNone(CoreAgent._requested_scope(state, elsewhere, a, proposal, NOW))
        self.assertIsNotNone(CoreAgent._requested_scope(state, conversation(ASK, HER_ASK, GO), a, proposal, NOW))
        self.assertIsNone(CoreAgent._requested_scope(state, conversation(ASK, HER_ASK, GO), a, proposal,
                                                     request.expires_at + timedelta(seconds=1)))

if __name__ == "__main__":
    unittest.main()
