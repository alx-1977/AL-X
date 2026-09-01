"""The notebook primitives are language-blind and decide nothing.

Each capability takes structured values and returns a structured result. None
reads AL/X's wording to work out what she meant, and none continues anything
after returning. The notebook records; the Core decides.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityResultState, SideEffect, ValueKind  # noqa: E402
from alx.research import SQLiteResearchStore  # noqa: E402
from alx.tools.notebook import DEFINITIONS, NotebookCapabilities  # noqa: E402


NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class NotebookCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.store = SQLiteResearchStore(Path(self._directory.name) / "r.db")
        self._moment = NOW
        self.capabilities = NotebookCapabilities(
            self.store, retention_days=365, clock=lambda: self._moment
        )

    def tearDown(self) -> None:
        self.store.close()
        self._directory.cleanup()

    def open(self) -> None:
        result = self.capabilities.open_thread("call-x", {
                "thread_id": "t-1",
                "question": "Why do some jellyfish appear not to age?",
                "interest": "I assumed ageing was universal and want to know why not.",
            }
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)

    def test_open_record_and_read_round_trip(self) -> None:
        self.open()
        self.capabilities.record_entry("call-x", {
                "entry_id": "e-1",
                "thread_id": "t-1",
                "kind": "hypothesis",
                "content": "Reversion is triggered by starvation.",
                "source_references": ["ev-1"],
            }
        )
        result = self.capabilities.read_thread("call-x", {"thread_id": "t-1"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["interest"][:8], "I assume")
        self.assertEqual(len(result.values["entries"]), 1)
        self.assertEqual(result.values["entries"][0]["kind"], "hypothesis")

    def test_revision_preserves_and_reports_the_new_version(self) -> None:
        self.open()
        self.capabilities.record_entry("call-x", {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim",
             "content": "First view."}
        )
        self._moment = NOW + timedelta(days=1)
        result = self.capabilities.revise_entry("call-x", {"entry_id": "e-1", "content": "Second view.",
             "reason": "New evidence.", "expected_revision": 1}
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["revision"], 2)
        self.assertEqual(self.store.read_entry("e-1").revisions[0].content, "First view.")

    def test_a_stale_revision_fails_without_overwriting(self) -> None:
        self.open()
        self.capabilities.record_entry("call-x", {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim", "content": "One."}
        )
        self.capabilities.revise_entry("call-x", {"entry_id": "e-1", "content": "Two.", "reason": "r", "expected_revision": 1}
        )
        result = self.capabilities.revise_entry("call-x", {"entry_id": "e-1", "content": "Three.", "reason": "r",
             "expected_revision": 1}
        )
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['code'], "revision_conflict")
        self.assertEqual(self.store.read_entry("e-1").current.content, "Two.")

    def test_correction_shares_the_revision_path(self) -> None:
        """One storage outcome, one implementation; two would drift apart."""
        self.open()
        self.capabilities.record_entry("call-x", {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim",
             "content": "Mistaken."}
        )
        result = self.capabilities.correct_entry("call-x", {"entry_id": "e-1", "content": "Corrected by Friedl.",
             "reason": "Factually wrong.", "expected_revision": 1}
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.capability_id, "correct_research_entry")
        self.assertEqual(len(self.store.read_entry("e-1").revisions), 2)

    def test_unscoped_search_is_refused_as_unusable_arguments(self) -> None:
        self.open()
        result = self.capabilities.search("call-x", {"query_id": "q-1"})
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['code'], "arguments_unusable")

    def test_search_returns_only_scoped_entries(self) -> None:
        self.open()
        self.capabilities.record_entry("call-x", {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim", "content": "In."}
        )
        result = self.capabilities.search("call-x", {"query_id": "q-1", "thread_ids": ["t-1"]})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual([e["entry_id"] for e in result.values["entries"]], ["e-1"])

    def test_delete_reports_removal_and_the_content_is_gone(self) -> None:
        self.open()
        self.capabilities.record_entry("call-x", {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim", "content": "Gone."}
        )
        result = self.capabilities.delete("call-x", {"record_id": "t-1", "kind": "thread"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["kind"], "thread")
        self.assertIs(
            self.capabilities.read_thread("call-x", {"thread_id": "t-1"}).state,
            CapabilityResultState.FAILED,
        )

    def test_missing_thread_reports_a_declared_failure_code(self) -> None:
        result = self.capabilities.read_thread("call-x", {"thread_id": "absent"})
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['code'], "thread_not_found")

    def test_every_failure_code_is_declared(self) -> None:
        declared = set(DEFINITIONS[0].possible_failure_codes)
        for definition in DEFINITIONS:
            self.assertEqual(set(definition.possible_failure_codes), declared)

    def test_there_are_exactly_eight_primitives(self) -> None:
        self.assertEqual(len(DEFINITIONS), 8)
        self.assertEqual(len({d.capability_id for d in DEFINITIONS}), 8)

    def test_no_capability_lists_the_whole_notebook(self) -> None:
        """A list-all primitive would defeat scoped retrieval entirely."""
        for definition in DEFINITIONS:
            self.assertNotIn("list_", definition.capability_id)
            self.assertNotIn("all_", definition.capability_id)

    def test_reads_are_marked_as_having_no_side_effect(self) -> None:
        by_id = {d.capability_id: d for d in DEFINITIONS}
        self.assertIs(by_id["search_research"].side_effect, SideEffect.NONE)
        self.assertIs(by_id["read_research_thread"].side_effect, SideEffect.NONE)

    def test_schemas_are_structured_objects_with_no_raw_language_field(self) -> None:
        """No capability takes an utterance, phrase, command or intent."""
        for definition in DEFINITIONS:
            self.assertIs(definition.input_schema.kind, ValueKind.OBJECT)
            for name in definition.input_schema.properties:
                for forbidden in ("utterance", "phrase", "command", "intent", "text_input"):
                    self.assertNotIn(forbidden, name)


if __name__ == "__main__":
    unittest.main()
