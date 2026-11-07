"""Expired mail content is neither reasoned over nor inherited (D-013).

On 2026-11-06 a conversation still held turns from September whose mail
content expired on 8 October. Every reply written there took that expiry, and
the screen withheld those replies as already expired.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, BackgroundEvent, ConversationOrigin, ConversationSnapshot, ConversationTurn,
    GoalState, Objective, RetentionPolicy, SuccessCriterion,
)
from alx.contracts.mail import MailReference  # noqa: E402
from alx.core.loop import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

NOW = datetime(2026, 11, 6, 16, 12, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)


class Queued:
    def __init__(self, *decisions):
        self.decisions, self.contexts = list(decisions), []

    def decide(self, context):
        self.contexts.append(context)
        return self.decisions.pop(0)


class ExpiredContentTests(unittest.TestCase):
    def test_a_reply_does_not_inherit_an_expiry_already_past(self) -> None:
        old_mail = RetentionPolicy().direct_mail(
            datetime(2026, 9, 8, tzinfo=UTC), (MailReference("INBOX", "1", "2"),))
        self.assertLess(old_mail.content_expires_at, NOW)
        conversation = ConversationSnapshot("runtime", (
            ConversationTurn("runtime", "old", ConversationOrigin.ALX_RESPONSE,
                             "Deleted the CodeRabbit emails.", datetime(2026, 9, 8, tzinfo=UTC),
                             None, old_mail),
            ConversationTurn("runtime", "now", ConversationOrigin.TYPED,
                             "How is the bill going?", NOW, "friedl"),
        ), 2, RETENTION)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            reasoner = Queued(AgentDecision(response="It's in."))
            outcome = CoreAgent(store, reasoner, lambda call, state: None, (),
                                clock=lambda: NOW).process(conversation, RETENTION, 2)
            store.close()
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        # The expired turn was not reasoned over...
        self.assertEqual([t.turn_id for t in reasoner.contexts[0].turns], ["now"])
        # ...and the reply's provenance carries no deadline already past.
        expires = outcome.response_provenance.content_expires_at
        self.assertTrue(expires is None or expires > NOW, expires)

    def test_an_expired_trigger_ends_the_turn_quietly(self) -> None:
        """Review of #215: the occasion's own content expired before it ran."""
        old_mail = RetentionPolicy().direct_mail(
            datetime(2026, 9, 8, tzinfo=UTC), (MailReference("INBOX", "1", "3"),))
        event = BackgroundEvent("mail:1:3", "mail.message_arrived",
                                datetime(2026, 9, 8, tzinfo=UTC), {"uid": "3"}, {}, old_mail)
        conversation = ConversationSnapshot("runtime", (), 1, RETENTION, (event,))
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            reasoner = Queued()
            outcome = CoreAgent(store, reasoner, lambda call, state: None, (),
                                clock=lambda: NOW).process(
                conversation, RETENTION, 2, trigger_event_id="mail:1:3")
            store.close()
        self.assertEqual((outcome.state, outcome.reason),
                         (CoreState.FINISHED_SILENTLY, "trigger_content_expired"))
        self.assertEqual(reasoner.contexts, [])

    def test_a_goal_whose_content_expired_is_not_offered(self) -> None:
        """Review of #215: selecting it would bring its expired deadline back."""
        old_mail = RetentionPolicy().direct_mail(
            datetime(2026, 9, 8, tzinfo=UTC), (MailReference("INBOX", "1", "4"),))
        with tempfile.TemporaryDirectory() as directory:
            now = [datetime(2026, 9, 8, tzinfo=UTC)]
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3", clock=lambda: now[0])
            goal = lambda gid: GoalState(gid, Objective("turn:t", "Tidy mail"),
                                         (SuccessCriterion("c", "done"),))
            store.create(goal("old"), "runtime", RETENTION, old_mail)
            now[0] = NOW
            store.create(goal("new"), "runtime", RETENTION)
            offered = [item.goal_id for item in store.list_unfinished("runtime")]
            needing = [item.goal_id for item in store.list_needing_core(10)]
            store.close()
        self.assertEqual(offered, ["new"])
        self.assertNotIn("old", needing)


if __name__ == "__main__":
    unittest.main()
