"""Finished work remains available through the one durable goal store (D-057)."""

import ast
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision, CapabilityCall, CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, Evidence, GoalMutationKind, GoalProposal, GoalState, GoalStatus,
    GoalStopReason, MailReference, Objective,
    ProgressRecord, Referent, RetentionPolicy, SideEffect, SuccessCriterion,
)
from alx.core import CoreAgent, CoreState
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.core.loop import RECENT_FINISHED_GOALS
from alx.core.model_reasoner import _context_payload, VERBATIM_HISTORY
from alx.goals import SQLiteGoalStore
from alx.goals.store import _goal_to_data
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.goal_history import DEFINITION, READ_OWN_GOALS, build_goal_history_executors

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)  # 10:00 SAST
RETENTION = NOW + timedelta(days=30)


def shipment(identifier="e8b6a256", status=GoalStatus.ACTIVE):
    return GoalState(
        identifier, Objective("event:reminder", "Track the FedEx shipment"),
        (SuccessCriterion("tracked", "Shipment status checked"),),
        context={"tracking_number": "878504110311", "occasion": "self-reminder"},
        referents=(Referent("parcel", {"carrier": "FedEx", "name": "Friedl"}),),
        progress=(ProgressRecord("checked", "Checked shipment", ("tracking",)),),
        evidence=(Evidence(
            "tracking", "shipment_status", {"status": "out for delivery", "location": "CAPE TOWN, WC"},
            ("tracked",), ("event:reminder",),
        ),),
        status=status,
        stop_reason={GoalStatus.COMPLETED: GoalStopReason.SUCCESS_CRITERIA_MET,
                     GoalStatus.CANCELLED: GoalStopReason.CANCELLED}.get(status),
    )


class RecordingReasoner:
    def __init__(self, *decisions):
        self.contexts = []
        self.decisions = list(decisions) or [AgentDecision(response="Checked.")]

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class FinishedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.now = NOW
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())

    def read(self, **arguments):
        return build_goal_history_executors(
            self.store, lambda: "history-call", lambda: self.now,
        )[READ_OWN_GOALS](arguments)

    def follow_up(self, reasoner=None, dispatch=None):
        reasoner = reasoner or RecordingReasoner()
        conversation = ConversationSnapshot("c1", (
            ConversationTurn("c1", "follow-up", ConversationOrigin.TYPED,
                             "Which address is it going to?", self.now, "friedl"),
        ), 1, RETENTION)
        core = CoreAgent(self.store, reasoner, dispatch or (lambda *_: self.fail("unexpected call")),
                         (DEFINITION,), clock=lambda: self.now)
        outcome = core.process(conversation, RETENTION, 5)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        return reasoner.contexts

    def test_reminder_completion_then_follow_up_after_restart(self):
        self.store.create(shipment(), "c1", RETENTION)
        completing = RecordingReasoner(
            AgentDecision(goal_id="e8b6a256", goal_proposal=GoalProposal(
                GoalMutationKind.REQUEST_COMPLETION,
            )),
            AgentDecision(response="Checked.", goal_id="e8b6a256"),
        )
        completion_contexts = self.follow_up(completing)
        self.assertEqual(self.store.load("e8b6a256").state.status, GoalStatus.COMPLETED)
        self.assertEqual(completion_contexts[0].recent_finished_goals, ())
        self.assertEqual(completion_contexts[-1].recent_finished_goals[0].state.goal_id, "e8b6a256")
        self.store.close()
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.now += timedelta(minutes=1)
        context = self.follow_up()[0]
        self.assertEqual(context.unfinished_goals, ())
        self.assertEqual(context.recent_finished_goals[0].state, self.store.load("e8b6a256").state)
        payload = json.loads(_context_payload(context))["recent_finished_goals"][0]
        self.assertEqual(payload["context"]["tracking_number"], "878504110311")
        self.assertEqual(payload["objective"]["source_reference"], "event:reminder")
        self.assertEqual(payload["referents"][0]["attributes"]["carrier"], "FedEx")
        self.assertEqual(payload["progress"][0]["evidence_refs"], ["tracking"])
        self.assertEqual(payload["evidence"][0]["attributes"],
                         {"status": "out for delivery", "location": "CAPE TOWN, WC"})
        found = self.read(query="878504110311")
        self.assertEqual(found.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(found.values["goals"][0]["goal_id"], "e8b6a256")

    def test_context_is_scoped_bounded_and_ordered_by_completion_not_last_write(self):
        for index in range(RECENT_FINISHED_GOALS + 2):
            self.now = NOW + timedelta(seconds=index)
            self.store.create(shipment(str(index), GoalStatus.COMPLETED), "c1", RETENTION)
        self.store.create(shipment("foreign", GoalStatus.COMPLETED), "c2", RETENTION)
        self.store.create(shipment("open"), "c1", RETENTION)
        self.now += timedelta(seconds=1)
        old = self.store.load("0")
        self.store.replace(old.state, RETENTION, old.revision)
        context = self.follow_up()[0]
        self.assertEqual([item.state.goal_id for item in context.recent_finished_goals],
                         [str(index) for index in range(RECENT_FINISHED_GOALS + 1, 1, -1)])
        self.assertEqual([item.goal_id for item in context.unfinished_goals], ["open"])

    def test_ties_cancelled_goals_and_recompletion(self):
        self.store.create(shipment("a", GoalStatus.COMPLETED), "c1", RETENTION)
        self.store.create(shipment("b", GoalStatus.CANCELLED), "c1", RETENTION)
        self.assertEqual([s.state.goal_id for s in self.store.list_recent_finished("c1", 5)], ["b", "a"])
        saved = self.store.load("a")
        saved = self.store.replace(replace(saved.state, status=GoalStatus.ACTIVE, stop_reason=None), RETENTION, saved.revision)
        self.now += timedelta(seconds=1)
        self.store.replace(replace(saved.state, status=GoalStatus.COMPLETED, stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET), RETENTION, saved.revision)
        self.assertEqual(self.store.list_recent_finished("c1", 5)[0].state.goal_id, "a")

    def test_finished_progress_is_not_truncated(self):
        progress = tuple(ProgressRecord(str(i), f"progress {i}", ("tracking",))
                         for i in range(VERBATIM_HISTORY + 3))
        self.store.create(replace(shipment(status=GoalStatus.COMPLETED), progress=progress), "c1", RETENTION)
        payload = json.loads(_context_payload(self.follow_up()[0]))
        self.assertEqual(len(payload["recent_finished_goals"][0]["progress"]), len(progress))

    def test_searches_all_statuses_conversations_and_nested_stored_content(self):
        for status in (GoalStatus.ACTIVE, GoalStatus.COMPLETED, GoalStatus.CANCELLED):
            self.store.create(shipment(status.value, status), status.value, RETENTION)
        for text in ("878504110311", "friedl", "CAPE TOWN", "Checked shipment", "event:reminder"):
            with self.subTest(text=text):
                result = self.read(query=text)
                self.assertEqual(len(result.values["goals"]), 3)
        self.assertEqual(self.read(query="%_").values["goals"], ())
        self.assertEqual(self.read(query="' OR 1=1 --").values["goals"], ())
        self.assertEqual(self.read(query="nothing").values["goals"], ())
        bounded = self.read(query="FedEx", limit=2)
        self.assertEqual(len(bounded.values["goals"]), 2)
        self.assertTrue(bounded.values["truncated"])
        self.assertEqual([g["goal_id"] for g in bounded.values["goals"]], ["completed", "cancelled"])

    def test_search_casefolds_unicode_names_and_stored_text_after_restart(self):
        states = (
            replace(shipment("name"), referents=(
                Referent("recipient", {"name": "MÜLLER"}),
            )),
            replace(shipment("objective"), objective=Objective("event:reminder", "ÜBER delivery")),
            replace(shipment("context"), context={"address": "ZÜRICH", "optional": None}),
            replace(shipment("progress"), progress=(ProgressRecord("checked", "ÉTUDIÉ shipment"),)),
            replace(shipment("evidence"), evidence=(Evidence(
                "tracking", "shipment_status", {"street": "Straße"},
                ("tracked",), ("event:reminder",),
            ),)),
            shipment("ÄUFTRAG"),
        )
        for state in states:
            self.store.create(state, "c1", RETENTION)
        for restarted in (False, True):
            if restarted:
                self.store.close()
                self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
            for identifier, queries in (
                ("name", ("müller", "MÜLLER")),
                ("objective", ("über", "ÜBER")),
                ("context", ("zürich", "ZÜRICH")),
                ("progress", ("étudié", "ÉTUDIÉ")),
                ("evidence", ("STRASSE", "straße")),
                ("ÄUFTRAG", ("äuftrag", "ÄUFTRAG")),
            ):
                for query in queries:
                    with self.subTest(restarted=restarted, query=query):
                        result = self.read(query=query)
                        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                        self.assertEqual([g["goal_id"] for g in result.values["goals"]], [identifier])
            self.assertEqual(self.read(goal_id="ÄUFTRAG").values["goals"][0]["goal_id"], "ÄUFTRAG")
            self.assertEqual(self.read(goal_id="äuftrag").failure["code"], "goal_not_found")

    def test_id_read_is_full_and_read_only(self):
        state = shipment(status=GoalStatus.COMPLETED)
        saved = self.store.create(state, "c1", RETENTION)
        read = self.read(goal_id=state.goal_id)
        self.assertEqual(json.loads(json.dumps(read.values["goals"][0]["state"], default=dict)),
                         _goal_to_data(state))
        self.assertFalse(read.values["truncated"])
        self.assertEqual(self.store.load(state.goal_id), saved)
        self.assertEqual(self.read(goal_id="missing").failure["code"], "goal_not_found")
        self.assertEqual(DEFINITION.side_effect, SideEffect.NONE)
        self.assertTrue(DEFINITION.output_schema.accepts(read.values))
        self.assertEqual(read.durable_values["goal_ids"], (state.goal_id,))
        self.assertNotIn("state", str(read.durable_values))

    def test_core_can_search_then_read_through_the_registered_broker(self):
        self.store.create(shipment(status=GoalStatus.COMPLETED), "another-conversation", RETENTION)
        current = [None]
        broker = CapabilityBroker(
            CapabilityRegistry((DEFINITION,)),
            SafetyGate({READ_OWN_GOALS: AuthorityPolicy(frozenset({"self.read"}))}),
            build_goal_history_executors(self.store, lambda: current[0], lambda: self.now),
        )
        def dispatch(call, state):
            current[0] = call.call_id
            return broker.dispatch(call, AuthorityContext("friedl", frozenset({"self.read"}), self.now))
        reasoner = RecordingReasoner(
            AgentDecision(call=CapabilityCall("search", READ_OWN_GOALS, {"query": "878504110311"})),
            AgentDecision(call=CapabilityCall("read", READ_OWN_GOALS, {"goal_id": "e8b6a256"})),
            AgentDecision(response="Checked the stored shipment evidence."),
        )
        contexts = self.follow_up(reasoner, dispatch)
        self.assertEqual(contexts[0].recent_finished_goals, ())
        self.assertEqual(contexts[1].transient_attempts[0].result.values["goals"][0]["goal_id"], "e8b6a256")
        self.assertEqual(contexts[2].transient_attempts[1].result.values["goals"][0]["state"]["context"]["tracking_number"], "878504110311")

    def test_invalid_requests_are_structured_failures(self):
        for arguments in ({}, {"query": ""}, {"goal_id": " "}, {"query": "x", "goal_id": "x"},
                          {"query": "x", "limit": 0}, {"query": "x", "limit": 51},
                          {"query": "x", "limit": True}, {"query": None}, {"extra": 1}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.read(**arguments).failure["code"], "arguments_unusable")

    def test_retention_and_provenance_survive_both_history_surfaces(self):
        provenance = RetentionPolicy().direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION, provenance)
        self.assertEqual(self.read(query="FedEx").provenance.content_expires_at,
                         provenance.content_expires_at)
        self.assertEqual(self.read(goal_id="e8b6a256").provenance.mail_references, provenance.mail_references)
        context = self.follow_up()[0]
        self.assertEqual(context.recent_finished_goals[0].provenance, provenance)
        self.now = RETENTION + timedelta(seconds=1)
        self.assertEqual(self.read(query="FedEx").values["goals"], ())
        self.assertEqual(self.read(goal_id="e8b6a256").failure["code"], "goal_not_found")
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())

    def test_finished_content_provenance_is_kept_when_selecting_an_open_goal(self):
        provenance = RetentionPolicy().direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION, provenance)
        self.store.create(shipment("open"), "c1", RETENTION)
        reasoner = RecordingReasoner(AgentDecision(
            goal_id="open", response="Checked.", goal_proposal=GoalProposal(
                GoalMutationKind.UPDATE, context={"tracking_number": "878504110311"},
            ),
        ))
        self.follow_up(reasoner)
        self.assertEqual(self.store.load("open").provenance.content_expires_at,
                         provenance.content_expires_at)

    def test_v8_migration_does_not_invent_completion_time(self):
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION)
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP INDEX goals_finished_by_conversation")
            connection.execute("ALTER TABLE goals DROP COLUMN finished_at")
            connection.execute("PRAGMA user_version = 8")
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.now += timedelta(seconds=1)
        self.store.create(shipment("new", GoalStatus.COMPLETED), "c1", RETENTION)
        self.assertEqual([s.state.goal_id for s in self.store.list_recent_finished("c1", 5)], ["new", "e8b6a256"])

    def test_live_registration_uses_the_existing_store_once(self):
        # The production composition must pass its existing goal_store, with
        # one registration and the same plain self.read permission.
        source = Path(__file__).resolve().parents[1] / "src/alx/bootstrap/live_voice.py"
        tree = ast.parse(source.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "build_goal_history_executors"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[0]), "goal_store")
        self.assertEqual(source.read_text().count('register((GOAL_HISTORY_DEFINITION,),'), 1)
        self.assertIn('policies[GOAL_HISTORY_DEFINITION.capability_id] = AuthorityPolicy(frozenset({"self.read"}))', source.read_text())


if __name__ == "__main__":
    unittest.main()
