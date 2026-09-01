"""The notebook reaches AL/X through the one capability path, and no other.

These tests drive the real registry, safety gate and broker rather than calling
the store directly, because the thing worth proving is that research is reached
the same way mail and Xero are: one registration, one dispatcher, one authority
check. A second way in would be a Law 0 violation the store's own tests could
never catch.

The scenario is the one that matters for continuity: AL/X opens a thread during
an ordinary conversation, records why it interests her, revises a view, the
process stops, and a fresh runtime picks the thread up and carries on.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.notebook import (  # noqa: E402
    RESEARCH_DELETE_PERMISSION,
    build_notebook_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
)
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402


NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class NotebookRuntimeTestCase(unittest.TestCase):
    """A runtime assembled exactly as live_voice assembles it."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self._call_id = [""]
        self.runtime = self._build()

    def tearDown(self) -> None:
        self.runtime.store.close()
        self._directory.cleanup()

    def _build(self):
        runtime = build_notebook_runtime(
            self.root, retention_days=3650, call_id_source=lambda: self._call_id[0]
        )
        self.registry = CapabilityRegistry()
        for definition in runtime.definitions:
            self.registry.register(definition)
        self.broker = CapabilityBroker(
            self.registry, SafetyGate(dict(runtime.policies)), dict(runtime.executors)
        )
        self.permissions = set(runtime.permissions)
        return runtime

    def authority(self, extra: set[str] | None = None) -> AuthorityContext:
        return AuthorityContext(
            principal_reference="friedl",
            granted_permission_references=frozenset(self.permissions | (extra or set())),
            evaluated_at=NOW,
        )

    def call(self, call_id: str, capability_id: str, arguments: dict, **kw):
        self._call_id[0] = call_id
        return self.broker.dispatch(
            CapabilityCall(call_id, capability_id, arguments), self.authority(**kw)
        )


class RuntimeVisibilityTest(NotebookRuntimeTestCase):
    def test_all_eight_capabilities_are_in_the_one_catalogue(self) -> None:
        registered = {d.capability_id for d in self.registry.list_definitions()}
        self.assertEqual(
            registered,
            {
                "open_research_thread",
                "record_research_entry",
                "revise_research_entry",
                "search_research",
                "read_research_thread",
                "set_research_status",
                "correct_research_entry",
                "delete_research",
            },
        )

    def test_research_is_reached_only_through_the_broker(self) -> None:
        """One registration, one dispatcher: no second notebook access path."""
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertEqual(source.count("build_notebook_runtime("), 1)
        # The store is constructed once, in the notebook bootstrap, and nowhere
        # else in the runtime.
        self.assertNotIn("SQLiteResearchStore(", source)


class ConversationScenarioTest(NotebookRuntimeTestCase):
    """The six steps: decide, record why, revise, stop, restart, continue."""

    def test_alx_opens_records_revises_restarts_and_continues(self) -> None:
        # 1 & 2: she decides to open a thread and records why it interests her.
        opened = self.call(
            "call-1",
            "open_research_thread",
            {
                "thread_id": "t-jelly",
                "question": "Why do some jellyfish appear not to age?",
                "interest": (
                    "I assumed biological ageing was universal. Being wrong "
                    "about something that basic is worth understanding."
                ),
            },
        )
        self.assertIs(opened.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(opened.result.state, CapabilityResultState.SUCCEEDED)
        self.assertIn("I assumed", opened.result.values["interest"])

        # 3: she adds a claim and a doubt, then revises the claim.
        self.call(
            "call-2",
            "record_research_entry",
            {
                "entry_id": "e-1",
                "thread_id": "t-jelly",
                "kind": "claim",
                "content": "Turritopsis dohrnii is biologically immortal.",
                "source_references": ["ev-1996"],
            },
        )
        self.call(
            "call-3",
            "record_research_entry",
            {
                "entry_id": "e-2",
                "thread_id": "t-jelly",
                "kind": "doubt",
                "content": "Every source traces to one lab population.",
            },
        )
        revised = self.call(
            "call-4",
            "revise_research_entry",
            {
                "entry_id": "e-1",
                "content": "It reverts to a polyp; 'immortal' overstates it.",
                "reason": "Reverting is not the same as not ageing.",
                "expected_revision": 1,
            },
        )
        self.assertEqual(revised.result.values["revision"], 2)

        # 4 & 5: the process stops and a fresh runtime starts on the same disk.
        self.runtime.store.close()
        self.runtime = self._build()

        # 6: she retrieves the thread and continues from it.
        found = self.call(
            "call-5", "search_research", {"query_id": "q-1", "thread_ids": ["t-jelly"]}
        )
        self.assertIs(found.result.state, CapabilityResultState.SUCCEEDED)
        entries = {e["entry_id"]: e for e in found.result.values["entries"]}
        self.assertEqual(set(entries), {"e-1", "e-2"})
        self.assertEqual(entries["e-1"]["revision"], 2)
        self.assertIn("overstates", entries["e-1"]["content"])

        thread = self.call("call-6", "read_research_thread", {"thread_id": "t-jelly"})
        self.assertIn("I assumed", thread.result.values["interest"])

        continued = self.call(
            "call-7",
            "record_research_entry",
            {
                "entry_id": "e-3",
                "thread_id": "t-jelly",
                "kind": "question",
                "content": "Does reversion happen outside captivity?",
            },
        )
        self.assertIs(continued.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(
            len(self.runtime.store.read_thread("t-jelly").entries), 3
        )

    def test_pausing_and_resuming_survives_restart(self) -> None:
        self.call(
            "call-1",
            "open_research_thread",
            {"thread_id": "t-1", "question": "Q?", "interest": "Because."},
        )
        self.call(
            "call-2", "set_research_status", {"thread_id": "t-1", "status": "paused"}
        )
        self.runtime.store.close()
        self.runtime = self._build()
        read = self.call("call-3", "read_research_thread", {"thread_id": "t-1"})
        self.assertEqual(read.result.values["status"], "paused")
        resumed = self.call(
            "call-4", "set_research_status", {"thread_id": "t-1", "status": "open"}
        )
        self.assertEqual(resumed.result.values["status"], "open")


class AuthorityTest(NotebookRuntimeTestCase):
    def test_alx_cannot_delete_her_own_research(self) -> None:
        """Deletion is irreversible, so the runtime never grants it."""
        self.assertNotIn(RESEARCH_DELETE_PERMISSION, self.permissions)
        self.call(
            "call-1",
            "open_research_thread",
            {"thread_id": "t-1", "question": "Q?", "interest": "Because."},
        )
        attempt = self.call(
            "call-2", "delete_research", {"record_id": "t-1", "kind": "thread"}
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertFalse(attempt.implementation_invoked)
        # The research is still there.
        self.assertEqual(self.runtime.store.read_thread("t-1").thread_id, "t-1")

    def test_correction_requires_approval(self) -> None:
        self.call(
            "call-1",
            "open_research_thread",
            {"thread_id": "t-1", "question": "Q?", "interest": "Because."},
        )
        self.call(
            "call-2",
            "record_research_entry",
            {"entry_id": "e-1", "thread_id": "t-1", "kind": "claim", "content": "X."},
        )
        attempt = self.call(
            "call-3",
            "correct_research_entry",
            {
                "entry_id": "e-1",
                "content": "Corrected.",
                "reason": "Wrong.",
                "expected_revision": 1,
            },
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)

    def test_reading_research_needs_only_read_authority(self) -> None:
        self.call(
            "call-1",
            "open_research_thread",
            {"thread_id": "t-1", "question": "Q?", "interest": "Because."},
        )
        reader = AuthorityContext(
            principal_reference="friedl",
            granted_permission_references=frozenset({"research.read"}),
            evaluated_at=NOW,
        )
        self._call_id[0] = "call-2"
        attempt = self.broker.dispatch(
            CapabilityCall("call-2", "read_research_thread", {"thread_id": "t-1"}),
            reader,
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)

    def test_writing_research_is_refused_without_write_authority(self) -> None:
        reader = AuthorityContext(
            principal_reference="friedl",
            granted_permission_references=frozenset({"research.read"}),
            evaluated_at=NOW,
        )
        self._call_id[0] = "call-1"
        attempt = self.broker.dispatch(
            CapabilityCall(
                "call-1",
                "open_research_thread",
                {"thread_id": "t-1", "question": "Q?", "interest": "Because."},
            ),
            reader,
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)


class ContextEfficiencyTest(NotebookRuntimeTestCase):
    def test_an_unscoped_retrieval_returns_nothing_to_the_core(self) -> None:
        """No dispatch can pour the whole notebook into a reasoning turn."""
        for index in range(5):
            self.call(
                f"open-{index}",
                "open_research_thread",
                {
                    "thread_id": f"t-{index}",
                    "question": f"Question {index}?",
                    "interest": f"Interest {index}.",
                },
            )
            self.call(
                f"entry-{index}",
                "record_research_entry",
                {
                    "entry_id": f"e-{index}",
                    "thread_id": f"t-{index}",
                    "kind": "claim",
                    "content": f"Claim {index}.",
                },
            )
        attempt = self.call("call-x", "search_research", {"query_id": "q-1"})
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")

    def test_scoped_retrieval_returns_only_the_named_thread(self) -> None:
        for index in range(3):
            self.call(
                f"open-{index}",
                "open_research_thread",
                {
                    "thread_id": f"t-{index}",
                    "question": f"Q{index}?",
                    "interest": f"I{index}.",
                },
            )
            self.call(
                f"entry-{index}",
                "record_research_entry",
                {
                    "entry_id": f"e-{index}",
                    "thread_id": f"t-{index}",
                    "kind": "claim",
                    "content": f"Claim {index}.",
                },
            )
        attempt = self.call(
            "call-x", "search_research", {"query_id": "q-1", "thread_ids": ["t-1"]}
        )
        self.assertEqual(
            [e["entry_id"] for e in attempt.result.values["entries"]], ["e-1"]
        )


class ResearchSpendingDisabledTest(unittest.TestCase):
    """Phase 3A wires storage only. Nothing may spend or run on its own."""

    def test_no_research_specialist_is_constructed_in_the_runtime(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertNotIn("ResearchSpecialist", source)
        self.assertNotIn("SQLiteResearchLedger", source)
        self.assertNotIn("ResearchBudget", source)

    def test_no_model_price_is_configured(self) -> None:
        from alx.observability import pricing

        self.assertEqual(pricing.USD_PER_MILLION, {})

    def test_the_notebook_bootstrap_schedules_nothing(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "notebook.py"
        ).read_text()
        # Match imports and calls, not the prose that says it schedules nothing.
        for forbidden in (
            "import asyncio",
            "import threading",
            "import sched",
            "Timer(",
            "create_task",
            "call_later",
        ):
            self.assertNotIn(forbidden, source)

    def test_the_notebook_costs_nothing_to_use(self) -> None:
        """No notebook capability reaches a model."""
        source = (REPOSITORY_ROOT / "src" / "alx" / "tools" / "notebook.py").read_text()
        for forbidden in ("ReasoningModel", "complete(", "Specialist", "cost_usd"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
