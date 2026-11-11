"""Production notebook composition has one brokered, approval-aware path."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.research import NOTEBOOK_PERMISSION  # noqa: E402
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import CapabilityCall, CapabilityResultState  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from research_actions import DELETE, OPEN_THREAD, RECORD_ENTRY, research_call, research_runtime  # noqa: E402


NOW = datetime(2026, 9, 2, tzinfo=UTC)


class NotebookRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.call_id = "call-open"
        self.runtime = research_runtime(Path(self.directory.name), lambda: self.call_id)
        self.broker = CapabilityBroker(
            CapabilityRegistry(self.runtime.definitions),
            SafetyGate(self.runtime.policies),
            self.runtime.executors,
        )
        self.authority = AuthorityContext(
            "friedl", frozenset({NOTEBOOK_PERMISSION}), NOW
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.directory.cleanup()

    def test_live_runtime_constructs_one_sqlite_store_and_persists_through_broker(self) -> None:
        opened = self.broker.dispatch(research_call(
            "call-open", OPEN_THREAD,
            {"thread_id": "thread-1", "question": "Why?", "interest": "Curiosity"},
        ), self.authority)
        self.assertIs(opened.result.state, CapabilityResultState.SUCCEEDED)

        self.call_id = "call-record"
        recorded = self.broker.dispatch(research_call(
            "call-record", RECORD_ENTRY,
            {
                "entry_id": "entry-1", "thread_id": "thread-1",
                "kind": "conclusion", "content": "A durable finding.",
            },
        ), self.authority)
        self.assertIs(recorded.result.state, CapabilityResultState.SUCCEEDED)
        from alx.research import SQLiteResearchStore
        reopened = SQLiteResearchStore(
            Path(self.directory.name) / "research-notebook.sqlite3"
        )
        try:
            self.assertEqual(
                reopened.read_entry("entry-1").current.content,
                "A durable finding.",
            )
        finally:
            reopened.close()

    def test_deleting_her_own_notebook_needs_no_approval(self) -> None:
        """D-051: her notebook is hers; Friedl's corrections stay his to approve."""
        attempt = self.broker.dispatch(research_call(
            "call-delete", DELETE,
            {"record_id": "thread-1", "kind": "thread"},
        ), self.authority)
        self.assertNotEqual(attempt.reason_code, "approval_required")


if __name__ == "__main__":
    unittest.main()
