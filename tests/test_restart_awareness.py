"""AL/X knows what she is running, and a restart wakes her once.

On 2026-10-06 she merged an invoice fix, retried it on the code already
loaded, and diagnosed the old failure as a new one. After a restart she then
waited, because nothing told her the restart had happened.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ReasoningContext  # noqa: E402
from alx.contracts.cognition import CognitionOrigin  # noqa: E402
from alx.continuity.ledger import SQLiteOpportunityLedger  # noqa: E402
from alx.continuity.runtime_source import (  # noqa: E402
    RUNTIME_CONVERSATION_ID,
    RuntimeStartedSource,
)
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS, _context_payload  # noqa: E402

STARTED = datetime(2026, 10, 6, 13, 13, tzinfo=UTC)


class RuntimeStartedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = SQLiteOpportunityLedger(Path(directory.name) / "ledger.sqlite3")

    def test_one_start_is_one_occasion_offered_once(self) -> None:
        source = RuntimeStartedSource(self.ledger, STARTED, enabled=True)
        (occasion,) = source.due_opportunities()
        self.assertIs(occasion.origin, CognitionOrigin.EXTERNAL_EVENT)
        self.assertEqual(occasion.conversation_id, RUNTIME_CONVERSATION_ID)
        self.assertTrue(occasion.opportunity_id.startswith("runtime-started:"))
        self.assertTrue(source.owns(occasion))
        self.assertTrue(source.claim(occasion))
        self.assertEqual(source.due_opportunities(), ())

    def test_disabled_offers_nothing(self) -> None:
        self.assertEqual(
            RuntimeStartedSource(self.ledger, STARTED).due_opportunities(), ()
        )

    def test_an_earlier_start_is_never_offered_again(self) -> None:
        earlier = RuntimeStartedSource(self.ledger, STARTED, enabled=True)
        (old,) = earlier.due_opportunities()
        earlier.claim(old)
        later = RuntimeStartedSource(
            self.ledger, STARTED.replace(hour=14), enabled=True,
        )
        self.assertEqual(later.recover(), (old.opportunity_id,))
        (offered,) = later.due_opportunities()
        self.assertNotEqual(offered.opportunity_id, old.opportunity_id)


class RuntimeFactsTests(unittest.TestCase):
    def test_the_runtime_facts_reach_her_context(self) -> None:
        facts = {
            "started_at": "2026-10-06T12:40:25+00:00",
            "running_commit": "b6d348e",
            "main_commit": "0e3b944",
        }
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=None, turns=(), capabilities=(),
            conversation_id="conversation-1", runtime=facts,
        )))
        self.assertEqual(payload["runtime"], facts)

    def test_the_protocol_explains_restarts_and_stale_state(self) -> None:
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        for stated in (
            "Merged code is not live until the process restarts",
            "event:runtime-started is this process starting",
            "before telling Friedl that something is still outstanding, check its current state",
            "An evidence item that also cites an attempt that failed or only partly succeeded counts for nothing",
        ):
            with self.subTest(stated=stated):
                self.assertIn(stated, text)


class RunningCommitTests(unittest.TestCase):
    def test_a_snapshot_names_its_own_commit(self) -> None:
        from alx.bootstrap.live_voice import running_commit_of

        class Checkout:
            def head_commit(self) -> str:
                return "f" * 40

        sha = "b914786be172cf32dbc7c96b460ad4f5ca1ca4ba"
        snapshot = Path("/repo/.git/alx-runtime") / sha
        self.assertEqual(running_commit_of(snapshot, Checkout()), sha)
        self.assertEqual(running_commit_of(Path("/repo"), Checkout()), "f" * 40)
        self.assertEqual(running_commit_of(Path("/repo"), None), "")


class CoreRuntimeFactsTests(unittest.TestCase):
    def test_facts_are_read_each_turn_and_a_failed_read_does_not_end_it(self) -> None:
        from alx.contracts import AgentDecision
        from alx.core import CoreAgent, CoreState
        from alx.goals import SQLiteGoalStore
        from tests.test_goal_context_selection import RETENTION, Queued, conversation

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            reasoner = Queued(AgentDecision(response="Noted."))
            CoreAgent(
                store, reasoner, lambda call, state: None, (),
                runtime_facts=lambda: {"running_commit": "abc"},
            ).process(conversation(), RETENTION, 5)
            self.assertEqual(dict(reasoner.contexts[0].runtime), {"running_commit": "abc"})

            def broken():
                raise OSError("git unavailable")

            reasoner = Queued(AgentDecision(response="Noted."))
            outcome = CoreAgent(
                store, reasoner, lambda call, state: None, (), runtime_facts=broken,
            ).process(conversation(), RETENTION, 5)
            self.assertEqual(outcome.state, CoreState.RESPONDED)
            self.assertEqual(dict(reasoner.contexts[0].runtime), {})
            store.close()


if __name__ == "__main__":
    unittest.main()
