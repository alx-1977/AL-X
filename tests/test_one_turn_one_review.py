"""One instruction from Friedl authorises one external review.

On 2026-09-06 Friedl asked for one review. Two `/review` comments reached
GitHub eleven seconds apart, and two reviews were paid for. Nothing was
broken in the watcher: the Core called `request_external_review` twice inside
a single turn's agent loop.

The single-use approval rule did not stop it. An approval is spent when it is
claimed, but the Core proposed a *fresh* approval in a later step of the same
turn, citing the same person turn again. Both approvals were unused, both were
grounded in Friedl's latest turn, and both passed.

What is spent is the instruction, not the identifier. So a capability whose
policy requires an approval grounded in Friedl's turn may be dispatched at
most once per turn, and a second one needs him to ask again.

The rule is read from the authority policies rather than named per capability:
`approval_required` already marks exactly the actions that need his word each
time. A capability that merely accepts an approval is untouched, because for
those a repeat is ordinary work rather than a second authorised action - the
web-search reformulation incident is the reason that distinction is kept.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision,
    ApprovalProposal,
    ApprovalScope,
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    ConversationOrigin,
    ConversationSnapshot,
    ConversationTurn,
    GoalMutationKind,
    GoalProposal,
    SuccessCriterion,
)
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.tools import ASK_WEB_SEARCH, WEB_SEARCH_DEFINITION
from alx.tools.review import DEFINITION as REVIEW_DEFINITION, REQUEST_EXTERNAL_REVIEW


NOW = datetime(2026, 9, 6, 20, 8, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)

# What the composition root derives from the policies: every capability whose
# AuthorityPolicy sets approval_required. Built the same way here.
TURN_BOUND = frozenset({REQUEST_EXTERNAL_REVIEW})


class Queued:
    """A fake Core model, following the harness the loop tests already use."""

    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)

    def decide(self, context):
        return self.decisions.pop(0)


def _turn(turn_id: str, origin: ConversationOrigin, content: str, person):
    return ConversationTurn(
        "conversation-1", turn_id, origin, content, NOW, person
    )


def one_instruction() -> ConversationSnapshot:
    """Friedl asks, once, for PR #21 to be reviewed."""
    return ConversationSnapshot(
        "conversation-1",
        (
            _turn("turn-1", ConversationOrigin.ALX_RESPONSE, "Ready.", None),
            _turn("turn-2", ConversationOrigin.TYPED,
                  "Please ask Qodo to review PR 21.", "friedl"),
        ),
        2,
        RETENTION,
    )


def a_second_instruction() -> ConversationSnapshot:
    """The same conversation, after Friedl asks again."""
    return ConversationSnapshot(
        "conversation-1",
        (
            _turn("turn-1", ConversationOrigin.ALX_RESPONSE, "Ready.", None),
            _turn("turn-2", ConversationOrigin.TYPED,
                  "Please ask Qodo to review PR 21.", "friedl"),
            _turn("turn-3", ConversationOrigin.ALX_RESPONSE, "Requested.", None),
            _turn("turn-4", ConversationOrigin.TYPED,
                  "Please review it again now.", "friedl"),
        ),
        4,
        RETENTION,
    )


def review_call(call_id: str, approval_id: str, number: int = 21):
    return CapabilityCall(
        call_id,
        REQUEST_EXTERNAL_REVIEW,
        {"pull_request_number": number},
        approval_id=approval_id,
    )


def approved(call: CapabilityCall, source: str) -> ApprovalProposal:
    return ApprovalProposal(
        call.approval_id, ApprovalScope(call.capability_id, call.arguments), source
    )


class OneTurnOneReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        # Every call that reached the provider. In the incident this was the
        # count that mattered: each entry is a `/review` comment on GitHub.
        self.dispatched: list[str] = []

    def agent(self, reasoner, capabilities=(REVIEW_DEFINITION,), goals=("goal-1",)):
        values = iter(goals)

        def dispatch(call, state):
            # Stands in for the provider. Nothing here contacts GitHub; the
            # point of the test is that this is never reached a second time.
            self.dispatched.append(call.capability_id)
            return CapabilityAttempt(
                call,
                CapabilityAttemptDisposition.EXECUTED,
                True,
                CapabilityResult(
                    call.call_id,
                    call.capability_id,
                    CapabilityResultState.SUCCEEDED,
                    {"requested": True},
                ),
            )

        return CoreAgent(
            self.store,
            reasoner,
            dispatch,
            capabilities,
            clock=lambda: NOW,
            identifier_factory=lambda: next(values),
            turn_bound_capabilities=TURN_BOUND,
        )

    @staticmethod
    def goal() -> GoalProposal:
        return GoalProposal(
            GoalMutationKind.CREATE,
            "Have PR 21 reviewed",
            (SuccessCriterion("criterion-1", "a review is requested"),),
        )

    def test_a_fresh_approval_cannot_buy_a_second_review_in_one_turn(self) -> None:
        """The incident, reproduced: one turn, two grounded approvals.

        Both approvals are new, both cite Friedl's latest turn, and neither has
        been used before - so every rule that existed at the time passed them.
        """
        first = review_call("call-1", "approve-1")
        second = review_call("call-2", "approve-2")
        reasoner = Queued(
            AgentDecision(
                call=first,
                approval_proposal=approved(first, "turn:turn-2"),
                goal_proposal=self.goal(),
            ),
            AgentDecision(
                call=second, approval_proposal=approved(second, "turn:turn-2")
            ),
            AgentDecision(response="Requested."),
        )

        outcome = self.agent(reasoner).process(one_instruction(), RETENTION, 6)

        # One GitHub contact, not two. This is the whole invariant.
        self.assertEqual(self.dispatched, [REQUEST_EXTERNAL_REVIEW])
        reasons = [
            item.reason_code
            for item in outcome.snapshot.state.attempts
            if item.disposition is CapabilityAttemptDisposition.REJECTED
        ]
        self.assertIn("approval_capability_already_dispatched", reasons)

    def test_the_second_request_is_refused_before_the_provider(self) -> None:
        """Refused before contact, not cleaned up afterwards.

        A review that is requested and then regretted has already been paid
        for, so the only useful place to stop it is before dispatch.
        """
        first = review_call("call-1", "approve-1")
        second = review_call("call-2", "approve-2", number=22)
        seen: list[int] = []

        def dispatch(call, state):
            seen.append(call.arguments["pull_request_number"])
            return CapabilityAttempt(
                call,
                CapabilityAttemptDisposition.EXECUTED,
                True,
                CapabilityResult(
                    call.call_id,
                    call.capability_id,
                    CapabilityResultState.SUCCEEDED,
                    {"requested": True},
                ),
            )

        reasoner = Queued(
            AgentDecision(
                call=first,
                approval_proposal=approved(first, "turn:turn-2"),
                goal_proposal=self.goal(),
            ),
            AgentDecision(
                call=second, approval_proposal=approved(second, "turn:turn-2")
            ),
            AgentDecision(response="Requested."),
        )
        agent = CoreAgent(
            self.store,
            reasoner,
            dispatch,
            (REVIEW_DEFINITION,),
            clock=lambda: NOW,
            identifier_factory=lambda: "goal-1",
            turn_bound_capabilities=TURN_BOUND,
        )
        agent.process(one_instruction(), RETENTION, 6)

        # A different pull request is still a second review on one instruction.
        self.assertEqual(seen, [21])

    def test_a_new_instruction_authorises_the_next_review(self) -> None:
        """The rule spends the turn, so a new turn restores the authority."""
        first = review_call("call-1", "approve-1")
        reasoner = Queued(
            AgentDecision(
                call=first,
                approval_proposal=approved(first, "turn:turn-2"),
                goal_proposal=self.goal(),
            ),
            AgentDecision(response="Requested."),
        )
        agent = self.agent(reasoner)
        agent.process(one_instruction(), RETENTION, 4)
        self.assertEqual(self.dispatched, [REQUEST_EXTERNAL_REVIEW])

        # Friedl asks again. The same goal, the same capability, a new turn.
        again = review_call("call-3", "approve-3")
        second_reasoner = Queued(
            # The same goal, selected rather than created: the authority was
            # spent by the previous turn, not by the goal.
            AgentDecision(
                goal_id="goal-1",
                call=again,
                approval_proposal=approved(again, "turn:turn-4"),
            ),
            AgentDecision(response="Requested again."),
        )
        agent_two = CoreAgent(
            self.store,
            second_reasoner,
            lambda call, state: self._executed(call),
            (REVIEW_DEFINITION,),
            clock=lambda: NOW,
            identifier_factory=lambda: "goal-2",
            turn_bound_capabilities=TURN_BOUND,
        )
        agent_two.process(a_second_instruction(), RETENTION, 4)

        # Two reviews in total: one per instruction, which is the point.
        self.assertEqual(
            self.dispatched, [REQUEST_EXTERNAL_REVIEW, REQUEST_EXTERNAL_REVIEW]
        )

    def _executed(self, call):
        self.dispatched.append(call.capability_id)
        return CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            CapabilityResult(
                call.call_id,
                call.capability_id,
                CapabilityResultState.SUCCEEDED,
                {"requested": True},
            ),
        )

    def test_a_capability_that_needs_no_approval_is_not_bound(self) -> None:
        """Reformulating a search is ordinary work, not a second authorisation.

        Eight refused reformulations on 2026-09-05 are why this distinction is
        kept: binding every capability that carries an approval would recreate
        that incident under a new name.
        """
        calls = [
            CapabilityCall(
                f"call-s{index}",
                ASK_WEB_SEARCH,
                {"search_id": f"s{index}", "subject": f"phrasing {index}"},
                approval_id=f"approve-s{index}",
            )
            for index in (1, 2, 3)
        ]
        reasoner = Queued(
            AgentDecision(
                call=calls[0],
                approval_proposal=approved(calls[0], "turn:turn-2"),
                goal_proposal=self.goal(),
            ),
            *[
                AgentDecision(
                    call=item, approval_proposal=approved(item, "turn:turn-2")
                )
                for item in calls[1:]
            ],
            AgentDecision(response="Answered."),
        )
        self.agent(reasoner, capabilities=(WEB_SEARCH_DEFINITION,)).process(
            one_instruction(), RETENTION, 8
        )

        self.assertEqual(self.dispatched, [ASK_WEB_SEARCH] * 3)


class PolicyDerivationTests(unittest.TestCase):
    """The bound set is read from the policies, not written down twice."""

    def test_requesting_a_review_is_turn_bound_by_its_own_policy(self) -> None:
        from alx.bootstrap.review import build_review_runtime

        class Provider:
            reviewer = "qodo"

            def request(self, review):  # pragma: no cover - never called
                raise AssertionError("no provider contact in this test")

        runtime = build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1", provider=Provider()
        )
        derived = frozenset(
            capability_id
            for capability_id, policy in runtime.policies.items()
            if policy.approval_required
        )
        self.assertEqual(derived, {REQUEST_EXTERNAL_REVIEW})


if __name__ == "__main__":
    unittest.main()
