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
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (AgentDecision,
                           CapabilityCall, CapabilityResultState, ConversationOrigin,
                           ConversationSnapshot, ConversationTurn, Evidence, GoalMutationKind,
                           GoalProposal, SuccessCriterion)
from alx.core import CoreAgent, CoreState
from alx.goals import SQLiteGoalStore
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.ship24 import TRACK_SHIPMENT
from alx.tools.web import ASK_WEB_PAGE, DEFINITION as WEB_DEFINITION, build_web_executors
from tests.test_ship24_tracking import TRACKED, response
from tests.test_web_conversation import StubFetcher

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

    def agent(self, reasoner, store=None, fetcher=None):
        definitions = self.runtime.definitions + ((WEB_DEFINITION,) if fetcher else ())
        policies = dict(self.runtime.policies)
        executors = dict(self.runtime.executors)
        permissions = self.runtime.permissions
        if fetcher:
            policies[ASK_WEB_PAGE] = AuthorityPolicy(frozenset({"web.read"}))
            executors.update(build_web_executors(fetcher, lambda: self.call_id))
            permissions |= frozenset({"web.read"})
        broker = CapabilityBroker(CapabilityRegistry(definitions), SafetyGate(policies), executors)

        def dispatch(call, state):
            self.call_id = call.call_id
            return broker.dispatch(call, AuthorityContext("friedl", permissions, NOW, state.approvals))
        return CoreAgent(store or self.store, reasoner, dispatch, definitions,
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
            self.agent(reasoner).process(conversation("Check parcel 1Z999AA10123456784"), RETENTION, 3)
        self.store.close()
        self.runtime = build_ship24_runtime(lambda: self.call_id, {"SHIP24_API_KEY": "example-token"})
        reopened = SQLiteGoalStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.load("goal-1").state.attempts[-1].call.capability_id, TRACK_SHIPMENT)
        follow_up = Queued(AgentDecision(goal_id="goal-1"),
                           AgentDecision(response="That was delivered.", goal_id="goal-1"))
        with patch("alx.providers.ship24.httpx.post") as post:
            follow_up_outcome = self.agent(follow_up, reopened).process(
                conversation("And where is it?", "turn-2"), RETENTION, 3)
        self.assertEqual(follow_up_outcome.state, CoreState.RESPONDED)
        self.assertEqual(follow_up.contexts[-1].active_goal.attempts[-1].result.durable_values["status_milestone"],
                         "delivered")
        post.assert_not_called()
        correction = Queued(AgentDecision(call=self.call("CORRECTED", "call-2"), goal_id="goal-1"),
                            AgentDecision(response="I checked the corrected number."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            self.agent(correction, reopened).process(conversation("Sorry, use CORRECTED", "turn-3"), RETENTION, 3)
        self.assertEqual(post.call_args.kwargs["json"]["trackingNumber"], "CORRECTED")
        self.assertEqual(len(reopened.list_goals()), 1)
        self.assertEqual([attempt.call.arguments["tracking_number"]
                          for attempt in reopened.load("goal-1").state.attempts],
                         ["1Z999AA10123456784", "CORRECTED"])

    def test_interruption_then_resumption_uses_the_same_shipment_attempt(self):
        first = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                       AgentDecision(response="The parcel is delivered."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            self.agent(first).process(conversation("Track my parcel 1Z999AA10123456784"), RETENTION, 3)
            interruption = Queued(AgentDecision(response="It is four o'clock."))
            self.agent(interruption).process(conversation("What time is it?", "turn-2"), RETENTION, 2)
            resumed = Queued(AgentDecision(goal_id="goal-1"),
                             AgentDecision(response="Back to the parcel: delivered.", goal_id="goal-1"))
            outcome = self.agent(resumed).process(conversation("Back to that parcel", "turn-3"), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertIsNone(interruption.contexts[0].active_goal)
        self.assertEqual(resumed.contexts[-1].active_goal.attempts[-1].call.call_id, "call-1")
        self.assertEqual(len(self.store.list_goals()), 1)
        post.assert_called_once()

    def test_shipment_and_courier_page_compose_inside_one_goal(self):
        fetcher = StubFetcher()
        reasoner = Queued(
            AgentDecision(call=self.call(), goal_proposal=goal()),
            AgentDecision(call=CapabilityCall("call-web-1", ASK_WEB_PAGE,
                                              {"page_id": "courier", "url": fetcher.page().requested_url})),
            AgentDecision(response="The parcel was delivered; I checked the courier page too."),
        )
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            outcome = self.agent(reasoner, fetcher=fetcher).process(
                conversation("Track my parcel and check the courier's service bulletin"), RETENTION, 4)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual([attempt.call.capability_id for attempt in self.store.load("goal-1").state.attempts],
                         [TRACK_SHIPMENT, ASK_WEB_PAGE])
        post.assert_called_once()
        self.assertEqual(len(fetcher.calls), 1)

    def test_success_is_verified_before_completion_and_cites_ship24(self):
        reasoner = Queued(
            AgentDecision(call=self.call(), goal_proposal=goal()),
            AgentDecision(response="Ship24 reports delivery.", goal_proposal=GoalProposal(
                GoalMutationKind.REQUEST_COMPLETION,
                new_evidence=(Evidence("evidence-1", "shipment_tracking",
                                       {"status_milestone": "delivered"},
                                       supports=("criterion-1",),
                                       source_references=("attempt:call-1",)),),
            ), response_requires_goal_commit=True),
        )
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            outcome = self.agent(reasoner).process(
                conversation("Check whether my parcel has arrived"), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(reasoner.contexts[-1].active_goal.attempts[-1].result.values["status_milestone"],
                         "delivered")
        state = self.store.load("goal-1").state
        self.assertEqual(state.evidence[-1].source_references, ("attempt:call-1",))
        self.assertEqual(state.status.value, "completed")
        post.assert_called_once()

    def test_standing_authority_needs_no_per_call_approval(self):
        self.assertFalse(self.runtime.policies[TRACK_SHIPMENT].approval_required)
        reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                          AgentDecision(response="The parcel is delivered."))
        with patch("alx.providers.ship24.httpx.post", return_value=response(200, TRACKED)) as post:
            outcome = self.agent(reasoner).process(conversation("Track my parcel"), RETENTION, 3)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertFalse(self.store.load("goal-1").state.approvals)
        post.assert_called_once()

    def test_failure_returns_to_core_for_replanning(self):
        reasoner = Queued(AgentDecision(call=self.call(), goal_proposal=goal()),
                          AgentDecision(call=self.call("CORRECTED", "call-2")),
                          AgentDecision(response="The corrected parcel was delivered."))
        with patch("alx.providers.ship24.httpx.post", side_effect=[
                response(404, {"errors": [{"code": "parcel_not_found"}]}),
                response(200, TRACKED)]) as post:
            outcome = self.agent(reasoner).process(
                conversation("Check parcel 1Z999AA10123456784"), RETENTION, 4)
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
