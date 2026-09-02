"""Adversarial guards for research safety failures found in review."""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    ModelCompletion,
    ResearchQuestion,
    SpecialistQuestion,
)
from alx.observability import ConfiguredPricingWorstCase, pricing  # noqa: E402
from alx.observability.research_budget import (  # noqa: E402
    ResearchBudget,
    SQLiteResearchLedger,
)
from alx.observability.usage import SQLiteUsageRecorder  # noqa: E402
from alx.specialists import (  # noqa: E402
    ModelSpecialist,
    ResearchCeilingFailed,
    ResearchInputUnbounded,
    ResearchSpecialist,
    ResearchTierModel,
)
from alx.specialists.research import input_token_upper_bound  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


class RecordingModel:
    provider, model = "testvendor", "test-model"
    supports_bounded_research = True

    def __init__(self, usage=None, telemetry=None) -> None:
        self.calls = 0
        self.request = None
        self.usage = usage if usage is not None else {"input_tokens": 1, "output_tokens": 1}
        self.telemetry = telemetry

    def complete(self, request):
        self.calls += 1
        self.request = request
        if self.telemetry is not None:
            self.telemetry(
                request.affinity_key,
                {
                    "code": "reasoning.completed",
                    "provider": self.provider,
                    "model": self.model,
                    "input_tokens": self.usage.get("input_tokens", 0),
                    "output_tokens": self.usage.get("output_tokens", 0),
                    "kind": request.kind,
                    "tier": request.tier,
                    "reservation_id": request.reservation_id,
                    "reserved_usd": request.reserved_usd,
                },
            )
        return ModelCompletion(
            self.provider, self.model, {"finding": "answered"}, self.usage
        )


class ResearchSafetyMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.prices = dict(pricing.USD_PER_MILLION)
        pricing.USD_PER_MILLION[("testvendor", "test-model")] = (1.0, 0.1, 1.0)

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self.prices)
        self.directory.cleanup()

    def researcher(self, model, *, max_input=2_000, telemetry=None):
        ledger = SQLiteResearchLedger(
            self.root / "ledger.db", ResearchBudget(0.02, 0.01)
        )
        researcher = ResearchSpecialist(
            {Cognition.JUDGE: ResearchTierModel("testvendor", "test-model", model)},
            ledger,
            ConfiguredPricingWorstCase(),
            max_input,
            1_000,
            0.01,
            telemetry_sink=telemetry,
        )
        return researcher, ledger

    def question(self, instruction="Answer.", material="material"):
        return ResearchQuestion(
            SpecialistQuestion(
                "question-id", instruction, material, SCHEMA,
                material_limit=len(material),
            ),
            Cognition.JUDGE,
        )

    def test_instruction_and_schema_overhead_are_refused_before_dispatch(self) -> None:
        model = RecordingModel()
        researcher, ledger = self.researcher(model, max_input=1_000)
        with self.assertRaises(ResearchInputUnbounded):
            researcher.answer(self.question(instruction="I" * 5_000))
        self.assertEqual(model.calls, 0)
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_material_is_truncated_against_the_complete_request_bound(self) -> None:
        model = RecordingModel()
        researcher, _ledger = self.researcher(model, max_input=1_200)
        researcher.answer(self.question(material="M" * 10_000))
        self.assertLessEqual(input_token_upper_bound(model.request), 1_200)
        self.assertLess(len(model.request.messages[1].content), 10_000)

    def test_reported_overrun_fails_the_same_call_and_records_true_spend(self) -> None:
        model = RecordingModel({"input_tokens": 1_000_000, "output_tokens": 1})
        researcher, ledger = self.researcher(model)
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(self.question())
        self.assertGreater(ledger.committed_usd(), 0.02)
        self.assertGreater(ledger.overrun_usd(), 0.0)
        call = ledger.day()["calls"][0]
        self.assertEqual(call["outcome"], "failed")
        self.assertEqual(call["failure_code"], "cost_overrun")

    def test_generic_specialist_cannot_be_given_a_tier_map(self) -> None:
        self.assertNotIn("tiers", inspect.signature(ModelSpecialist).parameters)
        with self.assertRaises(TypeError):
            ModelSpecialist(RecordingModel(), tiers={Cognition.JUDGE: RecordingModel()})

    def test_generic_specialist_refuses_a_research_question(self) -> None:
        with self.assertRaises(TypeError):
            ModelSpecialist(RecordingModel()).answer(self.question())

    def test_research_path_refuses_an_ordinary_question(self) -> None:
        model = RecordingModel()
        researcher, ledger = self.researcher(model)
        with self.assertRaises(TypeError):
            researcher.answer(
                SpecialistQuestion("ordinary", "Extract.", "material", SCHEMA)
            )
        self.assertEqual(model.calls, 0)
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_ordinary_specialist_is_not_recorded_as_core(self) -> None:
        model = RecordingModel()
        ModelSpecialist(model).answer(
            SpecialistQuestion("ordinary", "Extract.", "material", SCHEMA)
        )
        self.assertEqual(model.request.kind, "specialist")

    def test_unreviewed_transport_cannot_enter_research(self) -> None:
        class Unreviewed:
            pass

        with self.assertRaises(ValueError):
            ResearchTierModel("testvendor", "test-model", Unreviewed())

    def test_one_reservation_produces_one_usage_lifecycle_row(self) -> None:
        usage = SQLiteUsageRecorder(self.root / "usage.db")
        model = RecordingModel(telemetry=usage.record)
        researcher, _ledger = self.researcher(model, telemetry=usage.record)
        researcher.answer(self.question(), task_id="task-id")
        database = sqlite3.connect(self.root / "usage.db")
        database.row_factory = sqlite3.Row
        try:
            rows = database.execute(
                "SELECT task_id, kind, tier, reservation_id, outcome "
                "FROM reasoning_calls WHERE reservation_id != ''"
            ).fetchall()
        finally:
            database.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0])["task_id"], "task-id")
        self.assertEqual(dict(rows[0])["kind"], "research")
        self.assertEqual(dict(rows[0])["outcome"], "succeeded")

    def test_settlement_records_the_model_that_actually_answered(self) -> None:
        class ResolvedModel(RecordingModel):
            def complete(self, request):
                self.calls += 1
                self.request = request
                return ModelCompletion(
                    "testvendor", "resolved-model", {"finding": "answered"},
                    {"input_tokens": 1, "output_tokens": 1},
                )

        pricing.USD_PER_MILLION[("testvendor", "resolved-model")] = (1.0, 0.1, 1.0)
        model = ResolvedModel()
        researcher, ledger = self.researcher(model)
        researcher.answer(self.question())
        call = ledger.day()["calls"][0]
        self.assertEqual(call["provider"], "testvendor")
        self.assertEqual(call["model"], "resolved-model")


class NotebookRestoredTest(unittest.TestCase):
    """The notebook is authorised, and there is exactly one path to it.

    This replaces a guard that asserted the notebook must not exist. That was a
    reasonable reading while runtime authorisation was withheld, but D-023
    authorises AL/X to create and maintain her own research threads, so absence
    is no longer the property to hold. What matters now is that the notebook is
    present, reachable one way, and still carries the fixes it was approved
    with.
    """

    def test_the_approved_notebook_modules_are_present(self) -> None:
        required = (
            REPOSITORY_ROOT / "src" / "alx" / "contracts" / "notebook.py",
            REPOSITORY_ROOT / "src" / "alx" / "tools" / "notebook.py",
            REPOSITORY_ROOT / "src" / "alx" / "research" / "store.py",
        )
        for path in required:
            self.assertTrue(path.exists(), f"{path.name} was removed")

    def test_there_is_exactly_one_notebook_storage_implementation(self) -> None:
        """A second store would be the competing path Law 0 forbids."""
        stores = [
            path
            for path in (REPOSITORY_ROOT / "src" / "alx").rglob("*.py")
            if "class SQLiteResearchStore" in path.read_text()
        ]
        self.assertEqual([path.name for path in stores], ["store.py"])

    def test_the_notebook_keeps_the_fixes_it_was_approved_with(self) -> None:
        """Restoration must not quietly drop a reviewed guarantee."""
        from alx.contracts.notebook import (
            MAX_RETRIEVAL_LIMIT,
            MAX_THREAD_ENTRIES,
            MAX_WINDOW_DAYS,
            ResearchQuery,
            RevisionAuthor,
        )
        from alx.research.store import SCHEMA_VERSION

        # Scoped retrieval, with hard bounds.
        self.assertEqual((MAX_RETRIEVAL_LIMIT, MAX_WINDOW_DAYS), (25, 90))
        self.assertEqual(MAX_THREAD_ENTRIES, 25)
        with self.assertRaises(ValueError):
            ResearchQuery(query_id="q")
        # Revision authorship, so a model revision is not read as a correction.
        self.assertEqual({item.value for item in RevisionAuthor}, {"alx", "friedl"})
        # The migration that keeps an existing database readable after restart.
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_d013_erasure_is_still_not_enabled(self) -> None:
        """D-013 reserves secure byte erasure for a separate decision."""
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "research" / "store.py"
        ).read_text()
        self.assertNotIn('PRAGMA secure_delete', source)


class ActivationBoundaryTest(unittest.TestCase):
    """Research may now be built, but only through the one budgeted route.

    This replaces a guard that required research to be entirely unbuildable.
    Friedl approved the first live test in D-023, so the property worth holding
    is no longer "nothing constructs research" but "nothing constructs it except
    the prepaid path, and only for tiers explicitly enabled".
    """

    def test_the_general_provider_root_still_builds_no_research_models(self) -> None:
        from alx.bootstrap.providers import RuntimeProviders

        fields = set(RuntimeProviders.__dataclass_fields__)
        self.assertNotIn("research_tiers", fields)
        self.assertNotIn("research_identity", fields)
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "providers.py"
        ).read_text()
        self.assertNotIn("ResearchSpecialist", source)
        self.assertNotIn("SQLiteResearchLedger", source)

    def test_only_the_research_bootstrap_composes_prepaid_research(self) -> None:
        """One construction site, so there is one place spend can begin."""
        built = [
            path.name
            for path in (REPOSITORY_ROOT / "src" / "alx" / "bootstrap").rglob("*.py")
            if "ResearchSpecialist(" in path.read_text()
        ]
        self.assertEqual(built, ["research.py"])

    def test_no_tier_is_built_without_being_explicitly_enabled(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "research.py"
        ).read_text()
        # Tier construction is driven by the enabled set, never by iterating
        # every configured tier.
        self.assertIn("settings.enabled_tiers", source)
        self.assertIn("if not settings.enabled_tiers", source)

    def test_research_foundation_contains_no_scheduler_or_agent_loop(self) -> None:
        paths = (
            REPOSITORY_ROOT / "src" / "alx" / "specialists" / "research.py",
            REPOSITORY_ROOT / "src" / "alx" / "observability" / "research_budget.py",
        )
        for path in paths:
            source = path.read_text()
            for forbidden in (
                "create_task(", "call_later(", "Timer(", "while True",
                "CapabilityBroker", "capability_registry", "tools=",
            ):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
