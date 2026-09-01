"""Autonomous research cannot cross Friedl's hard spending boundary.

The dangerous call is the last one. Output and reasoning tokens are what make a
research question expensive and they are unknown until the model has answered,
so a runtime that checked the budget beforehand and recorded the cost afterwards
would let one final expensive call land outside the ceiling. These tests prove
the reservation closes that gap, and that exhaustion stops research rather than
quietly buying something cheaper.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    Cognition,
    ModelCompletion,
    SpecialistError,
    SpecialistQuestion,
)
from alx.observability import pricing  # noqa: E402
from alx.contracts import ResearchModelUnpriced  # noqa: E402
from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.research_budget import (  # noqa: E402
    ResearchBudget,
    ResearchBudgetExceeded,
    SQLiteResearchLedger,
)
from alx.specialists import ModelSpecialist, ResearchSpecialist  # noqa: E402


# A small bound so the worst-case price fits every ceiling used below.
MAX_INPUT = 4_000
MAX_OUTPUT = 1_000
# Worst case at the test rate (10/1/50 per MTok) is 0.09 USD.
PER_REQUEST_MAX = 0.09

SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


def question(cognition: Cognition = Cognition.SURVEY) -> SpecialistQuestion:
    return SpecialistQuestion(
        question_id="research-question",
        instruction="Answer the question from the material.",
        material="Some bounded research material.",
        answer_schema=SCHEMA,
        cognition=cognition,
    )


class RecordingModel:
    """A model that returns a fixed answer and a controllable usage report."""

    def __init__(self, provider: str, model: str, usage: dict | None = None) -> None:
        self.provider = provider
        self.model = model
        self._usage = usage if usage is not None else {}
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            self.provider, self.model, {"finding": "answered"}, self._usage
        )


class FailingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise RuntimeError("provider unavailable")


class ResearchBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name) / "research.db"
        self._prices = dict(pricing.USD_PER_MILLION)
        # A deliberately expensive rate so a single call's real cost is easy to
        # distinguish from its reservation.
        pricing.USD_PER_MILLION[("testvendor", "test-model")] = (10.0, 1.0, 50.0)

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self._prices)
        self._directory.cleanup()

    def ledger(self, daily: float, per_request: float) -> SQLiteResearchLedger:
        return SQLiteResearchLedger(
            self.path, ResearchBudget(daily_usd=daily, per_request_max_usd=per_request)
        )

    def researcher(self, ledger, model, tier=Cognition.SURVEY):
        specialist = ModelSpecialist(model, tiers={tier: model})
        return (
            ResearchSpecialist(
                specialist,
                ledger,
                ConfiguredPricingWorstCase(),
                lambda _tier: (model.provider, model.model),
                MAX_INPUT,
                MAX_OUTPUT,
                PER_REQUEST_MAX,
            ),
            specialist,
        )

    def test_reservation_holds_the_worst_case_while_the_call_runs(self) -> None:
        """Mid-flight the whole worst case is withdrawn, not a nominal figure."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve(
            "survey", "testvendor", "test-model", worst_case_usd=0.09
        )
        self.assertAlmostEqual(ledger.committed_usd(), 0.09, places=6)
        self.assertAlmostEqual(ledger.remaining_usd(), 9.91, places=6)
        self.assertEqual(reservation.reserved_usd, 0.09)

    def test_a_reservation_above_the_per_request_ceiling_is_refused(self) -> None:
        """The worst case is checked against the ceiling before withdrawal."""
        ledger = self.ledger(daily=10.0, per_request=0.05)
        with self.assertRaises(ResearchBudgetExceeded):
            ledger.reserve(
                "judge", "testvendor", "test-model", worst_case_usd=0.09
            )
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_unused_reservation_returns_to_the_day(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve("survey", "testvendor", "test-model")
        settled = ledger.settle(reservation, 0.25)
        self.assertEqual(settled, 0.25)
        self.assertEqual(ledger.committed_usd(), 0.25)
        self.assertEqual(ledger.remaining_usd(), 9.75)

    def test_daily_ceiling_cannot_be_exceeded_by_a_final_expensive_call(self) -> None:
        """The whole point: the last call cannot overshoot the boundary.

        Five requests at a two-dollar maximum exactly consume a ten-dollar day.
        The sixth is refused before dispatch, whatever it might have cost.
        """
        ledger = self.ledger(daily=10.0, per_request=2.0)
        for _ in range(5):
            ledger.reserve("judge", "testvendor", "test-model")
        self.assertEqual(ledger.remaining_usd(), 0.0)
        with self.assertRaises(ResearchBudgetExceeded):
            ledger.reserve("judge", "testvendor", "test-model")

    def test_committed_spend_never_exceeds_the_daily_budget(self) -> None:
        """Reserve until refused, settling each at the maximum, and check."""
        ledger = self.ledger(daily=5.0, per_request=1.0)
        reservations = []
        while True:
            try:
                reservations.append(ledger.reserve("judge", "testvendor", "test-model"))
            except ResearchBudgetExceeded:
                break
        # Reservations are what the ceiling is enforced against; settling at
        # the reserved amount leaves the day exactly spent, not overspent.
        for reservation in reservations:
            ledger.settle(reservation, reservation.reserved_usd)
        self.assertLessEqual(ledger.committed_usd(), 5.0)
        self.assertEqual(ledger.remaining_usd(), 0.0)

    def test_an_overrun_is_recorded_without_breaching_the_day(self) -> None:
        """The day is charged what was withdrawn; the excess is a recorded fault.

        Charging the day the true figure would let one bad call blow through the
        ceiling, and clamping it silently would hide that a bound failed. The
        reservation caps the day, and the overrun is reported separately so
        research can stop.
        """
        ledger = self.ledger(daily=10.0, per_request=2.0)
        reservation = ledger.reserve("judge", "testvendor", "test-model")
        settled = ledger.settle(reservation, 7.5)
        self.assertEqual(settled, 2.0)
        self.assertEqual(ledger.committed_usd(), 2.0)
        self.assertAlmostEqual(ledger.overrun_usd(), 5.5, places=6)

    def test_exhaustion_stops_research_and_does_not_downgrade_the_tier(self) -> None:
        """No JUDGE to COMPARE to SURVEY slide when money runs short."""
        ledger = self.ledger(daily=0.09, per_request=0.09)
        judge = RecordingModel("testvendor", "test-model", {"output_tokens": 10})
        survey = RecordingModel("testvendor", "test-model", {"output_tokens": 10})
        specialist = ModelSpecialist(
            survey, tiers={Cognition.SURVEY: survey, Cognition.JUDGE: judge}
        )
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricingWorstCase(),
            lambda _tier: ("testvendor", "test-model"),
            MAX_INPUT, MAX_OUTPUT, PER_REQUEST_MAX,
        )
        researcher.answer(question(Cognition.JUDGE))
        self.assertEqual(judge.calls, 1)
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question(Cognition.JUDGE))
        # Refused outright: the cheaper tier was never asked to stand in.
        self.assertEqual(judge.calls, 1)
        self.assertEqual(survey.calls, 0)

    def test_exhaustion_does_not_fall_back_to_another_provider(self) -> None:
        ledger = self.ledger(daily=0.09, per_request=0.09)
        primary = RecordingModel("testvendor", "test-model", {"output_tokens": 5})
        other = RecordingModel("othervendor", "other-model", {"output_tokens": 5})
        pricing.USD_PER_MILLION[("othervendor", "other-model")] = (1.0, 1.0, 1.0)
        specialist = ModelSpecialist(primary, tiers={Cognition.SURVEY: primary})
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricingWorstCase(),
            lambda _tier: ("testvendor", "test-model"),
            MAX_INPUT, MAX_OUTPUT, PER_REQUEST_MAX,
        )
        researcher.answer(question())
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question())
        self.assertEqual(other.calls, 0)

    def test_unpriced_model_cannot_spend_and_is_refused_before_dispatch(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=1.0)
        model = RecordingModel("unpricedvendor", "mystery-model", {"output_tokens": 5})
        researcher, _ = self.researcher(ledger, model)
        with self.assertRaises(ResearchModelUnpriced):
            researcher.answer(question())
        self.assertEqual(model.calls, 0)
        self.assertEqual(ledger.committed_usd(), 0.0)

    def test_price_is_never_guessed_for_an_unknown_model(self) -> None:
        self.assertIsNone(pricing.price_of("testvendor", "not-configured"))
        self.assertFalse(pricing.is_priced("testvendor", "not-configured"))
        self.assertIsNone(
            pricing.cost_usd("testvendor", "not-configured", {"output_tokens": 1000})
        )

    def test_a_failed_call_keeps_its_full_reservation(self) -> None:
        """A failure is not proof the provider did not bill for the work."""
        ledger = self.ledger(daily=1.0, per_request=0.09)
        model = FailingModel()
        model.provider, model.model = "testvendor", "test-model"
        specialist = ModelSpecialist(model, tiers={Cognition.SURVEY: model})
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricingWorstCase(),
            lambda _tier: ("testvendor", "test-model"),
            MAX_INPUT, MAX_OUTPUT, PER_REQUEST_MAX,
        )
        with self.assertRaises(SpecialistError):
            researcher.answer(question())
        self.assertAlmostEqual(ledger.committed_usd(), 0.09, places=6)
        self.assertAlmostEqual(ledger.remaining_usd(), 0.91, places=6)

    def test_repeated_failures_cannot_outrun_the_daily_ceiling(self) -> None:
        """The hole this closes: unbounded failing paid calls in one day."""
        ledger = self.ledger(daily=0.18, per_request=0.09)
        model = FailingModel()
        model.provider, model.model = "testvendor", "test-model"
        specialist = ModelSpecialist(model, tiers={Cognition.SURVEY: model})
        researcher = ResearchSpecialist(
            specialist, ledger, ConfiguredPricingWorstCase(),
            lambda _tier: ("testvendor", "test-model"),
            MAX_INPUT, MAX_OUTPUT, PER_REQUEST_MAX,
        )
        for _ in range(2):
            with self.assertRaises(SpecialistError):
                researcher.answer(question())
        with self.assertRaises(ResearchBudgetExceeded):
            researcher.answer(question())

    def test_unmeasured_usage_settles_at_the_full_reservation(self) -> None:
        """An unmeasured call is not free; treating it so would uncap the day."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        model = RecordingModel("testvendor", "test-model", {})
        researcher, _ = self.researcher(ledger, model)
        researcher.answer(question())
        self.assertAlmostEqual(ledger.committed_usd(), 0.09, places=6)

    def test_measured_cost_is_recorded_and_the_remainder_returned(self) -> None:
        ledger = self.ledger(daily=10.0, per_request=2.0)
        # 1M output tokens at 50 USD/1M would be 50; 1000 tokens is 0.05.
        model = RecordingModel(
            "testvendor", "test-model", {"input_tokens": 1000, "output_tokens": 1000}
        )
        researcher, _ = self.researcher(ledger, model)
        researcher.answer(question())
        expected = round(1000 / 1e6 * 10.0 + 1000 / 1e6 * 50.0, 6)
        self.assertAlmostEqual(ledger.committed_usd(), expected, places=6)
        self.assertLess(ledger.committed_usd(), 2.0)

    def test_budget_survives_restart(self) -> None:
        """A restart must not hand AL/X a fresh day's money."""
        ledger = self.ledger(daily=10.0, per_request=2.0)
        ledger.reserve("survey", "testvendor", "test-model")
        reopened = self.ledger(daily=10.0, per_request=2.0)
        self.assertEqual(reopened.committed_usd(), 2.0)
        self.assertEqual(reopened.remaining_usd(), 8.0)

    def test_unreconciled_reservation_stays_withdrawn(self) -> None:
        """A crashed call keeps its money withdrawn rather than leaking it."""
        ledger = self.ledger(daily=4.0, per_request=2.0)
        ledger.reserve("judge", "testvendor", "test-model")
        reopened = self.ledger(daily=4.0, per_request=2.0)
        reopened.reserve("judge", "testvendor", "test-model")
        with self.assertRaises(ResearchBudgetExceeded):
            reopened.reserve("judge", "testvendor", "test-model")

    def test_a_budget_that_cannot_afford_one_request_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResearchBudget(daily_usd=1.0, per_request_max_usd=2.0)


if __name__ == "__main__":
    unittest.main()
