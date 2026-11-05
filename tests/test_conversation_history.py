"""read_own_conversations: AL/X reads what was said and replied in every thread.

On 2026-11-05 Friedl asked about results she had given him that morning. The
reply sat in an earlier conversation, which nothing let her read, so she made
fifteen lookups in her goals and trace and then said the values were gone.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityResultState, ContentOrigin, ContentProvenance, ConversationOrigin,
    ConversationTurn,
)
from alx.contracts.mail import MailReference  # noqa: E402
from alx.conversation import SQLiteConversationStore  # noqa: E402
from alx.tools.conversation_history import (  # noqa: E402
    DEFINITION, READ_OWN_CONVERSATIONS, build_conversation_history_executors,
)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
KEEP = NOW + timedelta(days=30)
ROOT = Path(__file__).resolve().parents[1]


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteConversationStore(Path(directory.name) / "conversations.sqlite3")
        self.addCleanup(self.store.close)
        self.revisions: dict[str, int] = {}

    def say(self, conversation_id: str, turn_id: str, origin: ConversationOrigin,
            content: str, at: datetime, provenance: ContentProvenance | None = None) -> None:
        if conversation_id not in self.revisions:
            self.revisions[conversation_id] = self.store.create(conversation_id, KEEP).revision
        snapshot = self.store.append(
            ConversationTurn(conversation_id, turn_id, origin, content, at, None, provenance),
            KEEP, self.revisions[conversation_id])
        self.revisions[conversation_id] = snapshot.revision

    def read(self, **arguments):
        return build_conversation_history_executors(
            self.store, lambda: "call-1", lambda: NOW)[READ_OWN_CONVERSATIONS](arguments)

    def test_a_reply_in_an_earlier_conversation_is_found(self) -> None:
        self.say("morning", "m1", ConversationOrigin.TYPED,
                 "Give me the inductance for all four coils.", NOW - timedelta(hours=4))
        self.say("morning", "m2", ConversationOrigin.ALX_RESPONSE,
                 "RF1: 1.830 µH, RF4: 1.551 µH", NOW - timedelta(hours=4, minutes=-1))
        self.say("afternoon", "a1", ConversationOrigin.TYPED,
                 "Where are the values?", NOW - timedelta(minutes=5))
        result = self.read(query="INDUCTANCE")
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual([turn["turn_id"] for turn in result.values["turns"]], ["m1"])
        self.assertEqual(result.values["turns"][0]["conversation_id"], "morning")
        result = self.read(query="µh")
        self.assertEqual(result.values["turns"][0]["origin"], "alx_response")
        self.assertIn("1.551", result.values["turns"][0]["text"])
        # The audit keeps which turns were read, not their words.
        self.assertEqual(dict(result.durable_values), {"turn_ids": ("m2",), "truncated": False})

    def test_without_a_query_the_window_is_read_newest_first_and_bounded(self) -> None:
        for index in range(5):
            self.say("c", f"t{index}", ConversationOrigin.TYPED, f"turn {index}",
                     NOW - timedelta(hours=5 - index))
        self.say("c", "old", ConversationOrigin.TYPED, "last month",
                 NOW - timedelta(days=20))
        result = self.read(limit=3)
        self.assertEqual([turn["turn_id"] for turn in result.values["turns"]], ["t4", "t3", "t2"])
        self.assertTrue(result.values["truncated"])
        everything = self.read(**{"from": (NOW - timedelta(days=30)).isoformat()})
        self.assertIn("old", [turn["turn_id"] for turn in everything.values["turns"]])
        one = self.read(conversation_id="c", query="turn 1")
        self.assertEqual([turn["turn_id"] for turn in one.values["turns"]], ["t1"])

    def test_expired_content_is_not_returned(self) -> None:
        expired = ContentProvenance(frozenset({ContentOrigin.MAIL_MESSAGE}),
                                    NOW - timedelta(days=2),
                                    (MailReference("INBOX", "1", "2"),),
                                    NOW - timedelta(hours=1))
        self.say("c", "gone", ConversationOrigin.ALX_RESPONSE, "expired words",
                 NOW - timedelta(days=1), expired)
        self.say("c", "kept", ConversationOrigin.ALX_RESPONSE, "kept words",
                 NOW - timedelta(hours=2))
        self.assertEqual([turn["turn_id"] for turn in self.read(query="words").values["turns"]],
                         ["kept"])

    def test_a_goal_audit_keeps_no_search_text(self) -> None:
        """Review of #204: the query can quote what was said, so it is not saved."""
        import json
        from alx.contracts import (
            CapabilityAttempt, CapabilityAttemptDisposition, GoalState, Objective,
            ReasoningContext, SuccessCriterion,
        )
        from alx.core.model_reasoner import _native_call
        from alx.goals import SQLiteGoalStore
        context = ReasoningContext(None, (), (DEFINITION,), conversation_id="c")
        call, _ = _native_call(context, {
            "call_id": "r1", "capability_id": READ_OWN_CONVERSATIONS,
            "arguments_json": json.dumps({"query": "the private words", "limit": 5}),
            "approval_id": None, "approval_source_reference": None})
        self.assertEqual(call.arguments["query"], "the private words")
        self.say("c", "t1", ConversationOrigin.TYPED, "the private words", NOW - timedelta(hours=1))
        result = build_conversation_history_executors(
            self.store, lambda: "r1", lambda: NOW)[READ_OWN_CONVERSATIONS](call.arguments)
        goal = GoalState("g1", Objective("turn:t1", "Find it"), (SuccessCriterion("f", "found"),),
                         attempts=(CapabilityAttempt(
                             call, CapabilityAttemptDisposition.EXECUTED, True, result),))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        goals = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3", clock=lambda: NOW)
        self.addCleanup(goals.close)
        goals.create(goal, "c", KEEP)
        stored = goals._connection.execute("SELECT state_json FROM goals").fetchone()[0]
        self.assertNotIn("private words", stored)
        self.assertIn('"limit":5', stored.replace(" ", ""))
        reloaded = goals.load("g1").state.attempts[0]
        self.assertEqual(dict(reloaded.call.arguments), {"limit": 5})
        self.assertEqual(list(reloaded.result.values.get("turn_ids", ())), ["t1"])

    def test_unusable_arguments_return_to_her(self) -> None:
        for arguments in ({"limit": 0}, {"limit": 51}, {"query": " "}, {"unknown": 1},
                          {"from": NOW.isoformat(), "to": (NOW - timedelta(hours=1)).isoformat()},
                          {"from": "yesterday"}):
            with self.subTest(arguments=arguments):
                result = self.read(**arguments)
                self.assertEqual(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], "arguments_unusable")

    def test_a_storage_failure_is_evidence_not_an_exception(self) -> None:
        class Broken:
            def search_turns(self, **_values):
                raise OSError("disk")
        result = build_conversation_history_executors(
            Broken(), lambda: "call-1", lambda: NOW)[READ_OWN_CONVERSATIONS]({})
        self.assertEqual(result.failure["code"], "unavailable")

    def test_it_is_read_only_and_registered_in_the_runtime(self) -> None:
        self.assertEqual(DEFINITION.side_effect.value, "none")
        source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text(encoding="utf-8")
        calls = [node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "build_conversation_history_executors"]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
