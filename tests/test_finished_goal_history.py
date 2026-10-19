"""Finished work remains available through the one durable goal store (D-057)."""

import ast
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    AgentDecision, CapabilityCall, CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, Evidence, GoalMutationKind, GoalProposal, GoalState, GoalStatus,
    GoalStopReason, MailReference, Objective,
    ProgressRecord, Referent, RetentionPolicy, SideEffect, SuccessCriterion, ContentOrigin,
)
from alx.core import CoreAgent, CoreState
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.core.loop import RECENT_FINISHED_GOALS
from alx.core.model_reasoner import _context_payload, VERBATIM_HISTORY
from alx.goals import SQLiteGoalStore
from alx.goals.store import _goal_to_data
from alx.contracts.provenance import provenance_to_storage
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.goal_history import DEFINITION, READ_OWN_GOALS, build_goal_history_executors
from alx.bootstrap.autonomous import AutonomousCognitionRunner
from alx.continuity import FutureCognitionSource, SQLiteContinuityStore, SQLiteOpportunityLedger
from alx.conversation import ConversationGateway, SQLiteConversationStore
from alx.contracts import (CapabilityAttempt, CapabilityAttemptDisposition, CapabilityDefinition,
                           CapabilityResult, ResponseDelivery, StructuredSchema, ValueKind)
from alx.tools.continuity import REQUEST_FUTURE_COGNITION, build_continuity_executors

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

    def assert_shipment_visible(self):
        for arguments in ({"query": "878504110311"}, {"goal_id": "e8b6"}):
            with self.subTest(arguments=arguments):
                result = self.read(**arguments)
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                self.assertEqual([g["goal_id"] for g in result.values["goals"]], ["e8b6a256"])
        with self.subTest(surface="recent_finished"):
            self.assertEqual([g.state.goal_id for g in self.store.list_recent_finished("c1", 5)],
                             ["e8b6a256"])

    def test_old_email_does_not_expire_a_new_finished_goal(self):
        policy = RetentionPolicy()
        email = policy.direct_mail(NOW - timedelta(days=41), (MailReference("INBOX", "42", "1"),))
        derived = policy.derive(ContentOrigin.ALX, NOW, (email,))
        self.assertLess(derived.content_expires_at, NOW)
        saved = self.store.create(shipment(), "c1", RETENTION, derived)
        self.store.replace(shipment(status=GoalStatus.COMPLETED), RETENTION, saved.revision)
        self.assert_shipment_visible()
        self.assertEqual(self.store.load("e8b6a256").provenance.content_expires_at, RETENTION)

    def test_invalid_stored_expiry_is_repaired_once_without_renewing_valid_rows(self):
        policy = RetentionPolicy()
        provenance = policy.direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION, provenance)
        valid = self.store.create(replace(shipment("valid", GoalStatus.COMPLETED), context={}),
                                  "elsewhere", RETENTION, provenance)
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT created_at FROM goals WHERE goal_id = 'e8b6a256'"
            ).fetchone()[0], NOW.isoformat())
            connection.execute("UPDATE goals SET content_expires_at = ? WHERE goal_id = ?",
                               ((NOW - timedelta(days=11)).isoformat(), "e8b6a256"))
            # The goal was created on October 19 after its inherited October 8
            # expiry. A later provenance write must not replace that evidence.
            connection.execute("UPDATE goals SET content_recorded_at = ? WHERE goal_id = ?",
                               ((NOW + timedelta(hours=1)).isoformat(), "e8b6a256"))
            connection.execute("PRAGMA user_version = 9")
        self.now += timedelta(days=1)
        for _ in range(2):
            self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
            self.assert_shipment_visible()
            repaired = self.store.load("e8b6a256")
            self.assertEqual(repaired.provenance.content_expires_at, RETENTION)
            self.assertEqual(self.store.load("valid"), valid)
            self.store.close()
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.now = RETENTION
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.read(goal_id="e8b6").failure["code"], "goal_not_found")
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())

    def test_goal_retention_clock_on_mail_introduction_and_subsequent_updates(self):
        policy = RetentionPolicy()
        for origin in (ContentOrigin.PERSON, ContentOrigin.ALX, ContentOrigin.EXTERNAL):
            with self.subTest(origin=origin):
                identifier = origin.value
                self.now = NOW
                saved = self.store.create(shipment(identifier), "c1", RETENTION,
                                          policy.non_mail(origin, NOW))
                self.assertIsNone(saved.provenance.content_expires_at)
                self.now += timedelta(days=2)
                email = policy.direct_mail(NOW - timedelta(days=41),
                                            (MailReference("INBOX", "42", "1"),))
                supplied = policy.derive(ContentOrigin.ALX, self.now, (saved.provenance, email))
                updated = self.store.replace(saved.state, RETENTION, saved.revision, supplied)
                self.assertEqual(updated.provenance.recorded_at, self.now)
                self.assertEqual(updated.provenance.content_expires_at, RETENTION)
                self.now += timedelta(days=3)
                supplied = policy.derive(ContentOrigin.ALX, self.now, (updated.provenance, email))
                rewritten = self.store.replace(updated.state, RETENTION, updated.revision, supplied)
                self.assertEqual(rewritten.provenance, updated.provenance)
                fresh_email = policy.direct_mail(self.now, (MailReference("INBOX", "42", "2"),))
                supplied = policy.derive(ContentOrigin.ALX, self.now, (rewritten.provenance, fresh_email))
                rewritten = self.store.replace(rewritten.state, RETENTION, rewritten.revision, supplied)
                self.assertEqual(rewritten.provenance.content_expires_at, RETENTION)
                self.assertEqual(set(rewritten.provenance.mail_references),
                                 set((*email.mail_references, *fresh_email.mail_references)))

    def test_goal_deadline_respects_own_horizon_and_creation_lower_bound(self):
        email = RetentionPolicy().direct_mail(NOW - timedelta(days=41),
                                              (MailReference("INBOX", "42", "1"),))
        for horizon, expected in ((-1, 0), (2, 2), (90, 30)):
            with self.subTest(horizon=horizon):
                saved = self.store.create(shipment(str(horizon)), "c1",
                                          NOW + timedelta(days=horizon), email)
                self.assertEqual(saved.provenance.recorded_at, NOW)
                self.assertEqual(saved.provenance.content_expires_at,
                                 NOW + timedelta(days=expected))

    def test_legacy_update_does_not_renew_elapsed_content_deadline(self):
        day_0 = NOW - timedelta(days=31)
        expiry = day_0 + timedelta(days=30)
        provenance = RetentionPolicy().direct_mail(
            day_0, (MailReference("INBOX", "42", "1"),))
        self.now = day_0
        # Cover both reliable creation evidence and a legacy row where only
        # the latest write is known. Neither allows renewal of this deadline.
        for identifier in ("known", "unknown"):
            saved = self.store.create(shipment(identifier), "c1", RETENTION, provenance)
            self.now = NOW
            self.store.replace(shipment(identifier, GoalStatus.COMPLETED),
                               RETENTION, saved.revision)
            self.now = day_0
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            # Earlier Core updates advanced provenance's timestamp without
            # changing the original content deadline.
            connection.execute("UPDATE goals SET content_recorded_at = ?",
                               (NOW.isoformat(),))
            connection.execute("UPDATE goals SET created_at = NULL WHERE goal_id = 'unknown'")
            connection.execute("PRAGMA user_version = 9")
        self.now = NOW
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        for identifier in ("known", "unknown"):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.store.load(identifier).provenance.content_expires_at, expiry)
                self.assertEqual(self.read(goal_id=identifier).failure["code"], "goal_not_found")
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())

    def test_legacy_repair_preserves_deadline_without_creation_evidence(self):
        policy = RetentionPolicy()
        provenance = policy.direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION, provenance)
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("ALTER TABLE goals DROP COLUMN created_at")
            connection.execute("UPDATE goals SET content_recorded_at = ?, content_expires_at = ?",
                               (NOW.isoformat().replace("+00:00", "Z"),
                                (NOW - timedelta(days=11)).isoformat()))
            connection.execute("PRAGMA user_version = 9")
        expiry = NOW - timedelta(days=11)
        self.now = NOW + timedelta(days=1)
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.load("e8b6a256").provenance.content_expires_at, expiry)
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.read(goal_id="e8b6").failure["code"], "goal_not_found")
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())

    def schema_9_database(self, identifiers=("e8b6a256",)):
        """Build the old schema directly, without ever creating created_at."""
        self.store.close()
        self.path.unlink()
        expiry = NOW - timedelta(days=11)
        provenance = replace(RetentionPolicy().direct_mail(
            NOW, (MailReference("INBOX", "42", "1"),)),
            recorded_at=NOW + timedelta(hours=1), content_expires_at=expiry)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE goals (goal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                "retention_until TEXT NOT NULL, state_json TEXT NOT NULL, conversation_id TEXT, "
                "content_origins TEXT, content_recorded_at TEXT, content_expires_at TEXT, "
                "mail_references TEXT, scope TEXT, updated_at TEXT, unfinished INTEGER, "
                "project_id TEXT, finished_at TEXT)"
            )
            for identifier in identifiers:
                connection.execute(
                    "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, 3, RETENTION.isoformat(),
                     json.dumps(_goal_to_data(shipment(identifier, GoalStatus.COMPLETED))),
                     "c1", *provenance_to_storage(provenance), None,
                     (NOW + timedelta(hours=1)).isoformat(), 0, None, NOW.isoformat()),
                )
            connection.execute("PRAGMA user_version = 9")
        self.now = NOW + timedelta(days=1)

    def creation_trace(self, records):
        # Exact persisted format from SQLiteTraceRecord, with explicit event
        # labels emitted by CoreAgent._persist_goal (references are shortened).
        with sqlite3.connect(self.path.with_name("trace.sqlite3")) as connection:
            connection.execute(
                "CREATE TABLE trace (at TEXT NOT NULL, subsystem TEXT, status TEXT, "
                "label TEXT, reference TEXT, reason_code TEXT, duration_ms INTEGER, count INTEGER)"
            )
            connection.executemany(
                "INSERT INTO trace VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)", records)

    def test_schema_9_upgrade_repairs_expiry_from_earliest_creation_trace(self):
        self.schema_9_database()
        self.creation_trace([
            ((NOW + timedelta(hours=1)).isoformat(), "goals", "completed", "Goal created", "e8b6a256"),
            (NOW.astimezone(timezone(timedelta(hours=2))).isoformat(),
             "goals", "completed", "Goal created", "e8b6a256"),
        ])
        for _ in range(2):
            self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
            self.assert_shipment_visible()
            self.assertEqual(self.store.load("e8b6a256").provenance.content_expires_at, RETENTION)
            with sqlite3.connect(self.path) as connection:
                self.assertEqual(datetime.fromisoformat(connection.execute(
                    "SELECT created_at FROM goals").fetchone()[0]), NOW)
            self.store.close()
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)

    def test_schema_9_update_trace_is_not_creation_evidence(self):
        self.schema_9_database()
        self.creation_trace([
            (NOW.isoformat(), "goals", "completed", "Goal completed", "e8b6a256"),
        ])
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.load("e8b6a256").provenance.content_expires_at,
                         NOW - timedelta(days=11))
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT created_at FROM goals").fetchone()[0])

    def test_schema_9_rejects_invalid_unrelated_and_ambiguous_creation_traces(self):
        self.schema_9_database(("e8b6a256", "e8b6a256-other", "invalid", "naive", "update", "foreign"))
        self.creation_trace([
            (NOW.isoformat(), "goals", "completed", "Goal created", "e8b6a256"),
            ("not-a-time", "goals", "completed", "Goal created", "invalid"),
            ("2026-10-19T08:00:00", "goals", "completed", "Goal created", "naive"),
            (NOW.isoformat(), "goals", "completed", "Goal completed", "update"),
            (NOW.isoformat(), "core", "completed", "Goal created", "foreign"),
            (NOW.isoformat(), "goals", "failed", "Goal created", "foreign"),
            (NOW.isoformat(), "goals", "completed", "Goal created", "missing"),
        ])
        with sqlite3.connect(self.path) as connection:
            before = connection.execute("SELECT * FROM goals ORDER BY goal_id").fetchall()
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.store.list_recent_finished("c1", 10), ())
        with sqlite3.connect(self.path) as connection:
            after = connection.execute("SELECT * FROM goals ORDER BY goal_id").fetchall()
        self.assertEqual(after, [(*row, None) for row in before])

    def test_schema_9_verified_creation_preserves_genuinely_elapsed_deadline(self):
        self.schema_9_database()
        created = NOW - timedelta(days=41)
        self.creation_trace([
            (created.isoformat(), "goals", "completed", "Goal created", "e8b6a256"),
            (NOW.isoformat(), "goals", "completed", "Goal created", "e8b6a256"),
        ])
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.load("e8b6a256").provenance.content_expires_at,
                         NOW - timedelta(days=11))
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(datetime.fromisoformat(connection.execute(
                "SELECT created_at FROM goals").fetchone()[0]), created)

    def test_history_deadlines_are_compared_as_instants(self):
        provenance = RetentionPolicy().direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        self.store.create(shipment(status=GoalStatus.COMPLETED), "c1", RETENTION, provenance)
        # Equivalent legacy ISO timestamps must not disappear two hours early.
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE goals SET retention_until = ?, content_expires_at = ?",
                               ("2026-11-18T06:00:00-02:00", "2026-11-18T06:00:00-02:00"))
        self.now = RETENTION - timedelta(minutes=30)
        self.assert_shipment_visible()
        self.now = RETENTION
        self.assertEqual(self.read(query="878504110311").values["goals"], ())
        self.assertEqual(self.read(goal_id="e8b6").failure["code"], "goal_not_found")
        self.assertEqual(self.store.list_recent_finished("c1", 5), ())

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

    def test_self_requested_completion_delivered_to_person_then_read_after_restart(self):
        root = Path(self.directory.name)
        continuity = SQLiteContinuityStore(root / "continuity.sqlite3")
        ledger = SQLiteOpportunityLedger(root / "occasions.sqlite3")
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        for store in (continuity, ledger, conversations):
            self.addCleanup(store.close)
        requested_at = NOW - timedelta(hours=1)
        request = build_continuity_executors(
            continuity, 30, lambda: "schedule", clock=lambda: requested_at,
            conversation_id_source=lambda: "originating-thread",
        )[REQUEST_FUTURE_COGNITION]({
            "request_id": "shipment-follow-up", "not_before": NOW.isoformat(),
            "note": "Check the parcel again",
        })
        self.assertEqual(request.state, CapabilityResultState.SUCCEEDED)
        identifier = "e8b6a256-0000-4000-8000-000000000001"
        evidence = Evidence("tracking", "shipment_status", {"status": "delivered"},
                            ("tracked",), ("attempt:track",))
        reasoner = RecordingReasoner(
            AgentDecision(goal_proposal=GoalProposal(
                GoalMutationKind.CREATE, objective_summary="Track the FedEx shipment",
                success_criteria=(SuccessCriterion("tracked", "Status checked"),),
                context={"occasion": "self-requested follow-up"},
                referents=(Referent("parcel", {"carrier": "FedEx"}),),
            ), call=CapabilityCall("track", "track_shipment", {"tracking_number": "878504110311"})),
            AgentDecision(response="The parcel was delivered.", goal_proposal=GoalProposal(
                GoalMutationKind.REQUEST_COMPLETION, new_evidence=(evidence,),
            ), response_requires_goal_commit=True),
        )
        definition = CapabilityDefinition(
            "track_shipment", "Read shipment status", StructuredSchema(ValueKind.OBJECT),
            StructuredSchema(ValueKind.OBJECT), SideEffect.NONE,
        )
        def dispatch(call, state):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.SUCCEEDED,
                                 {"tracking_number": "878504110311", "status": "delivered"}))
        core = CoreAgent(self.store, reasoner, dispatch, (definition,), clock=lambda: self.now,
                         identifier_factory=lambda: identifier)
        gateway = ConversationGateway(core, conversations, identifier_factory=lambda: "reply",
                                      clock=lambda: self.now)
        class Delivery:
            def deliver(_, conversation_id, response, opportunity_id):
                turn_id, _ = gateway.locate_reply(conversation_id, response)
                self.assertTrue(gateway.relay_response(conversation_id, turn_id, "c1"))
                return ResponseDelivery.DELIVERED
        source = FutureCognitionSource(continuity, ledger, enabled=True, clock=lambda: self.now)
        other = GoalState("other", Objective("event:other", "Other work"),
                          (SuccessCriterion("done", "Done"),), status=GoalStatus.CANCELLED,
                          stop_reason=GoalStopReason.CANCELLED)
        self.store.create(other, "originating-thread", RETENTION)
        self.store.create(replace(other, goal_id="cancelled"), "c1", RETENTION)
        runner = AutonomousCognitionRunner(source, ledger, gateway, 5, 30,
                                           response_transport=Delivery(), clock=lambda: self.now)
        self.assertTrue(runner.run_one(source.due_opportunities()[0]))
        saved = self.store.load(identifier)
        self.assertEqual(saved.state.status, GoalStatus.COMPLETED)
        self.assertEqual(saved.conversation_id, "originating-thread")
        self.assertEqual(conversations.load("c1").turns[0].content, "The parcel was delivered.")
        # The number occurs only in the persisted capability attempt, not the goal's narrative.
        self.assertNotIn("878504110311", str(saved.state.context))
        self.assertNotIn("878504110311", str(saved.state.evidence))
        self.store.close()
        self.store = SQLiteGoalStore(self.path, clock=lambda: self.now)
        conversations.close()
        conversations = SQLiteConversationStore(root / "conversations.sqlite3")
        self.addCleanup(conversations.close)
        person = RecordingReasoner()
        gateway = ConversationGateway(
            CoreAgent(self.store, person, dispatch, (DEFINITION,), clock=lambda: self.now),
            conversations, clock=lambda: self.now,
        )
        gateway.receive_conversation_turn(ConversationTurn("c1", "person", ConversationOrigin.TYPED,
                                        "What did you find?", self.now, "friedl"), 3, RETENTION)
        with self.subTest(surface="recent_finished_goals"):
            finished = person.contexts[0].recent_finished_goals
            self.assertEqual([g.state.goal_id for g in finished], [identifier, "cancelled"])
            self.assertEqual(finished[0].state.context, saved.state.context)
            self.assertEqual(finished[0].state.evidence, (evidence,))
        for query in ("FedEx", "shipment", "878504110311", identifier, "e8b6a256"):
            with self.subTest(query=query):
                self.assertEqual([g["goal_id"] for g in self.read(query=query).values["goals"]], [identifier])
        for goal_id in (identifier, "e8b6a256"):
            with self.subTest(goal_id=goal_id):
                result = self.read(goal_id=goal_id)
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
                self.assertEqual(result.values["goals"][0]["goal_id"], identifier)

    def test_id_prefixes_are_literal_unambiguous_and_retained(self):
        for identifier in ("abc-1", "abc-2", "literal%_id"):
            self.store.create(shipment(identifier, GoalStatus.COMPLETED), "c1", RETENTION)
        self.assertEqual(self.read(goal_id="abc").failure["code"], "arguments_unusable")
        self.assertEqual(self.read(goal_id="abc-1").values["goals"][0]["goal_id"], "abc-1")
        self.assertEqual(self.read(goal_id="literal%_").values["goals"][0]["goal_id"], "literal%_id")
        # Exact identities win even when they are prefixes of other identities.
        self.store.create(shipment("abc", GoalStatus.COMPLETED), "c1", RETENTION)
        self.assertEqual(self.read(goal_id="abc").values["goals"][0]["goal_id"], "abc")
        self.now = RETENTION + timedelta(seconds=1)
        self.assertEqual(self.read(goal_id="abc").failure["code"], "goal_not_found")

    def test_reply_links_preserve_bounds_isolation_and_retention(self):
        for index in range(RECENT_FINISHED_GOALS + 2):
            self.now = NOW + timedelta(seconds=index)
            self.store.create(shipment(str(index), GoalStatus.COMPLETED), "source", RETENTION + timedelta(days=1))
        self.store.create(shipment("unanswered", GoalStatus.COMPLETED), "source", RETENTION)
        links = tuple(str(index) for index in range(RECENT_FINISHED_GOALS + 2))
        recent = self.store.list_recent_finished("target", RECENT_FINISHED_GOALS,
                                                 response_goal_ids=(*links, *links, "missing"))
        self.assertEqual([g.state.goal_id for g in recent],
                         [str(i) for i in range(RECENT_FINISHED_GOALS + 1, 1, -1)])
        self.assertEqual(self.store.list_recent_finished("unrelated", 10), ())
        provenance = RetentionPolicy().direct_mail(NOW, (MailReference("INBOX", "42", "1"),))
        reasoner = RecordingReasoner()
        core = CoreAgent(self.store, reasoner, lambda *_: self.fail("unexpected dispatch"), (),
                         clock=lambda: self.now)
        reply = ConversationTurn("target", "reply", ConversationOrigin.ALX_RESPONSE,
                                 "Checked.", NOW, provenance=provenance, response_goal_id="0")
        def process_reply():
            core.process(ConversationSnapshot("target", (reply,), 1, RETENTION + timedelta(days=1)),
                         RETENTION + timedelta(days=1), 1)
            return reasoner.contexts[-1].recent_finished_goals
        self.assertEqual([g.state.goal_id for g in process_reply()], ["0"])
        self.now = provenance.content_expires_at
        # The linked goal itself can still exist, but an expired reply grants no visibility.
        reasoner.decisions.append(AgentDecision(response="Checked."))
        self.assertEqual(process_reply(), ())
        self.assertEqual(self.store.load("0").state.status, GoalStatus.COMPLETED)

    def test_v2_reply_without_goal_link_stays_unlinked(self):
        path = Path(self.directory.name) / "legacy-conversations.sqlite3"
        conversations = SQLiteConversationStore(path)
        conversations.create("c1", RETENTION)
        conversations.append(ConversationTurn("c1", "reply", ConversationOrigin.ALX_RESPONSE,
                                               "Checked.", NOW), RETENTION, 1)
        conversations.close()
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE conversation_turns SET turn_json = ?", (
                json.dumps(["c1", "reply", "alx_response", "Checked.", NOW.isoformat(), None]),))
            connection.execute("PRAGMA user_version = 2")
        conversations = SQLiteConversationStore(path)
        self.addCleanup(conversations.close)
        self.assertIsNone(conversations.load("c1").turns[0].response_goal_id)

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
