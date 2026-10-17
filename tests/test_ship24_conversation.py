"""Shipment tracking through the shared Core with Ship24 HTTP mocked."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.ship24 import build_ship24_runtime
from alx.contracts import (AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
                           CapabilityCall, CapabilityResultState, ConversationOrigin,
                           ConversationSnapshot, ConversationTurn, GoalMutationKind,
                           GoalProposal, SuccessCriterion)
from alx.core import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore
from alx.tools.ship24 import TRACK_SHIPMENT
from tests.test_ship24_tracking import TRACKED, response

NOW = datetime(2026, 10, 17, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


class Queued:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


def conversation(wording, turn="turn-1"):
    return ConversationSnapshot("conversation-1", (
        ConversationTurn("conversation-1", turn, ConversationOrigin.TYPED,
                         wording, NOW, "friedl"),), 1, RETENTION)


def goal():
    return GoalProposal(GoalMutationKind.CREATE, "Find parcel status", (
        SuccessCriterion("criterion-1", "the parcel status is checked"),))


class Ship24ConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)
        self.runtime = build_ship24_runtime(lambda: self.call_id, {"SHIP24_API_KEY": "example-token"})
        self.call_id = "call-1"

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def agent(self, reasoner, store=None):
        def dispatch(call, state):
            self.call_id = call.call_id
            result = self.runtime.executors[call.capability_id](call.arguments)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)
        return CoreAgent(store or self.store, reasoner, dispatch, self.runtime.definitions,
                         clock=lambda: NOW, identifier_factory=lambda: "goal-1")

    def call(self, number="1Z999AA10123456784", call_id="call-1"):
        return CapabilityCall(call_id, TRACK_SHIPMENT, {"tracking_number": number})

    def test_paraphrases_choose_tracking_and_core_receives_result(self):
        for index, wording in enumerate(("Track my parcel 1Z999AA10123456784",
                                         "Where has shipment 1Z999AA10123456784 got to?",
                                         "Could you check the delivery for 1Z999AA10123456784?")):
            with self.subTest(wording=wording):
                self.store.close()
                self.store = SQLiteGoalStore(Path(self.directory.name) / f"paraphrase-{index}.sqlite3")
                reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                                  AgentDecision(response="Ship24 reports delivery."))
                with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)):
                    outcome = self.agent(reasoner).process(conversation(wording), RETENTION, 3)
                self.assertEqual(outcome.state, CoreState.RESPONDED)
                self.assertEqual(reasoner.contexts[-1].active_goal.attempts[-1].result.values["status_milestone"], "delivered")

    def test_follow_up_correction_and_restart_use_durable_goal(self):
        reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                          AgentDecision(response="Ship24 reports delivery."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)):
            self.agent(reasoner).process(conversation("Check this parcel"), RETENTION, 3)
        self.store.close()
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.load("goal-1").state.attempts[-1].call.capability_id, TRACK_SHIPMENT)
        follow_up = Queued(AgentDecision(response="That was delivered." , goal_id="goal-1"))
        self.agent(follow_up, reopened).process(conversation("And where is it?", "turn-2"), RETENTION, 2)
        correction = Queued(AgentDecision(call=self.call("CORRECTED", "call-2"), goal_id="goal-1"),
                            AgentDecision(response="I checked the corrected number."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            self.agent(correction, reopened).process(conversation("Sorry, use CORRECTED", "turn-3"), RETENTION, 3)
        self.assertEqual(post.call_args.kwargs["json"]["trackingNumber"], "CORRECTED")

    def test_failure_returns_to_core_for_replanning(self):
        reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                          AgentDecision(call=self.call("CORRECTED", "call-2")),
                          AgentDecision(response="The corrected parcel was delivered."))
        with patch("alx.providers.ship24.httpx.post", side_effect=[
                response(404, {"errors": [{"code": "parcel_not_found"}]}), response(200, TRACKED)]) as post:
            outcome = self.agent(reasoner).process(conversation("Check this parcel"), RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(reasoner.contexts[1].active_goal.attempts[-1].result.failure["code"], "tracking_not_found")
        self.assertIs(reasoner.contexts[-1].active_goal.attempts[-1].result.state, CapabilityResultState.SUCCEEDED)

    def test_exhausted_allowance_reaches_core_and_repeated_plan_stops_http(self):
        reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                          AgentDecision(call=self.call("ANOTHER", "call-2")),
                          AgentDecision(response="The tracking allowance is used up."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(
                429, {"errors": [{"code": "plan_limit_exceeded"}]})) as post:
            outcome = self.agent(reasoner).process(conversation("Track both parcels"), RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(reasoner.contexts[-1].active_goal.attempts[-1].result.failure["code"],
                         "tracking_allowance_exhausted")


if __name__ == "__main__":
    unittest.main()
