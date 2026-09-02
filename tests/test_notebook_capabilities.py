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


def evidence_provenance(sources, now):
    """Stand-in for the runtime's evidence lookup.

    Anything named ev-mail-* is treated as derived from a mail message, so the
    D-013 deadline reaches research that cites it. Everything else is external
    evidence with no retention deadline of its own.
    """
    from alx.contracts.mail import MailReference
    from alx.contracts.provenance import ContentOrigin, RetentionPolicy

    policy = RetentionPolicy()
    found = []
    for reference in sources:
        if reference.startswith("ev-mail-"):
            found.append(
                policy.direct_mail(now, (MailReference("INBOX", "1", reference),))
            )
        else:
            found.append(policy.non_mail(ContentOrigin.EXTERNAL, now))
    return tuple(found)



class NotebookFixture(unittest.TestCase):
    """Shared setup only; assertions live in the classes below."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.store = SQLiteResearchStore(Path(self._directory.name) / "r.db")
        self._moment = NOW
        self.capabilities = NotebookCapabilities(
            self.store,
            retention_days=365,
            clock=lambda: self._moment,
            provenance_of=evidence_provenance,
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


class NotebookCapabilityTest(NotebookFixture):
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

    def test_durable_capability_receipts_never_copy_notebook_prose(self) -> None:
        opened = self.capabilities.open_thread("call-open", {
            "thread_id": "t-1",
            "question": "A private research question",
            "interest": "Her own reason for caring",
        })
        recorded = self.capabilities.record_entry("call-record", {
            "entry_id": "e-1",
            "thread_id": "t-1",
            "kind": "conclusion",
            "content": "The complete research finding belongs in the notebook.",
        })
        self.assertEqual(
            dict(opened.durable_values), {"thread_id": "t-1", "status": "open"}
        )
        self.assertEqual(
            dict(recorded.durable_values),
            {"entry_id": "e-1", "thread_id": "t-1", "revision": 1},
        )
        self.assertNotIn("content", recorded.durable_values)
        self.assertNotIn("question", opened.durable_values)
        self.assertNotIn("interest", opened.durable_values)

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


class ProvenanceThroughCapabilitiesTest(NotebookFixture):
    """D-013 must reach research written the way AL/X actually writes it.

    The store honoured provenance from the start, but the capability path
    discarded it, so a mail quotation written through a capability carried no
    origin and no deadline. Testing the store directly could never catch that.
    """

    def test_mail_derived_citation_inherits_the_d013_deadline(self) -> None:
        from alx.contracts.provenance import ContentOrigin, RetentionPolicy

        self.open()
        result = self.capabilities.record_entry(
            "call-x",
            {
                "entry_id": "e-1",
                "thread_id": "t-1",
                "kind": "claim",
                "content": "The supplier confirmed the revised date.",
                "source_references": ["ev-mail-42"],
            },
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        stored = self.store.read_entry("e-1").current.provenance
        self.assertIsNotNone(stored)
        self.assertIn(ContentOrigin.MAIL_MESSAGE, stored.origins)
        self.assertTrue(stored.governed_by_retention())
        self.assertEqual(
            stored.content_expires_at, RetentionPolicy().expires_at(NOW)
        )

    def test_research_of_her_own_carries_no_retention_deadline(self) -> None:
        from alx.contracts.provenance import ContentOrigin

        self.open()
        self.capabilities.record_entry(
            "call-x",
            {"entry_id": "e-1", "thread_id": "t-1", "kind": "doubt",
             "content": "This rests on one population."},
        )
        stored = self.store.read_entry("e-1").current.provenance
        self.assertEqual(stored.origins, frozenset({ContentOrigin.ALX}))
        self.assertIsNone(stored.content_expires_at)

    def test_an_unresolvable_citation_is_refused_not_written_unstamped(self) -> None:
        """The safe failure: refuse rather than create an undated mail record."""
        unresolved = NotebookCapabilities(
            self.store, retention_days=365, clock=lambda: self._moment
        )
        unresolved.open_thread(
            "c1", {"thread_id": "t-2", "question": "Q?", "interest": "Because."}
        )
        result = unresolved.record_entry(
            "c2",
            {"entry_id": "e-9", "thread_id": "t-2", "kind": "claim",
             "content": "Cites something.", "source_references": ["ev-mail-1"]},
        )
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "evidence_unresolved")

    def test_a_revision_citing_mail_also_inherits_the_deadline(self) -> None:
        self.open()
        self.capabilities.record_entry(
            "call-1",
            {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim",
             "content": "Her own reasoning."},
        )
        self.capabilities.revise_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Now supported by the message.",
             "reason": "Found the confirmation.", "expected_revision": 1,
             "source_references": ["ev-mail-7"]},
        )
        stored = self.store.read_entry("e-1").current.provenance
        self.assertTrue(stored.governed_by_retention())


class RevisionAuthorshipTest(NotebookFixture):
    """AL/X changing her mind and Friedl correcting the record are different."""

    def setUp(self) -> None:
        super().setUp()
        self.open()
        self.capabilities.record_entry(
            "call-1",
            {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim",
             "content": "First view."},
        )

    def test_alx_revises_her_own_conclusion_and_it_is_attributed_to_her(self) -> None:
        result = self.capabilities.revise_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Second view.",
             "reason": "New evidence.", "expected_revision": 1},
        )
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["author"], "alx")

    def test_alx_cannot_sign_her_revision_as_friedls_correction(self) -> None:
        """The author is fixed by the capability, never taken from arguments."""
        result = self.capabilities.revise_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Second view.",
             "reason": "New evidence.", "expected_revision": 1,
             "author": "friedl"},
        )
        # The argument is ignored, not honoured.
        self.assertEqual(result.values["author"], "alx")
        self.assertEqual(self.store.read_entry("e-1").current.author.value, "alx")

    def test_a_correction_is_recorded_as_friedls(self) -> None:
        result = self.capabilities.correct_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Corrected.",
             "reason": "Factually wrong.", "expected_revision": 1},
        )
        self.assertEqual(result.values["author"], "friedl")

    def test_history_distinguishes_her_revision_from_his_correction(self) -> None:
        self.capabilities.revise_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Her second view.",
             "reason": "Rethought it.", "expected_revision": 1},
        )
        self.capabilities.correct_entry(
            "call-3",
            {"entry_id": "e-1", "content": "His correction.",
             "reason": "Still wrong.", "expected_revision": 2},
        )
        authors = [r.author.value for r in self.store.read_entry("e-1").revisions]
        self.assertEqual(authors, ["alx", "alx", "friedl"])

    def test_neither_operation_overwrites_history(self) -> None:
        self.capabilities.revise_entry(
            "call-2",
            {"entry_id": "e-1", "content": "Her second view.",
             "reason": "Rethought it.", "expected_revision": 1},
        )
        self.capabilities.correct_entry(
            "call-3",
            {"entry_id": "e-1", "content": "His correction.",
             "reason": "Still wrong.", "expected_revision": 2},
        )
        contents = [r.content for r in self.store.read_entry("e-1").revisions]
        self.assertEqual(
            contents, ["First view.", "Her second view.", "His correction."]
        )


class RetrievalBoundsTest(NotebookFixture):
    def test_a_large_thread_is_paged_not_returned_whole(self) -> None:
        from alx.contracts.notebook import MAX_THREAD_ENTRIES

        self.open()
        for index in range(MAX_THREAD_ENTRIES + 10):
            self.capabilities.record_entry(
                f"call-{index}",
                {"entry_id": f"e-{index}", "thread_id": "t-1", "kind": "claim",
                 "content": f"Claim {index}."},
            )
        first = self.capabilities.read_thread("call-x", {"thread_id": "t-1"})
        self.assertEqual(len(first.values["entries"]), MAX_THREAD_ENTRIES)
        self.assertEqual(first.values["total_entries"], MAX_THREAD_ENTRIES + 10)
        self.assertEqual(first.values["next_offset"], MAX_THREAD_ENTRIES)
        second = self.capabilities.read_thread(
            "call-y", {"thread_id": "t-1", "offset": first.values["next_offset"]}
        )
        self.assertEqual(len(second.values["entries"]), 10)
        self.assertNotIn("next_offset", second.values)

    def test_revision_history_returned_to_the_core_is_bounded(self) -> None:
        from alx.contracts.notebook import MAX_ENTRY_REVISIONS

        self.open()
        self.capabilities.record_entry(
            "call-0",
            {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim",
             "content": "Version 1."},
        )
        for revision in range(1, MAX_ENTRY_REVISIONS + 5):
            self.capabilities.revise_entry(
                f"call-{revision}",
                {"entry_id": "e-1", "content": f"Version {revision + 1}.",
                 "reason": "again", "expected_revision": revision},
            )
        self.assertEqual(
            len(self.store.read_entry("e-1").revisions), MAX_ENTRY_REVISIONS
        )
