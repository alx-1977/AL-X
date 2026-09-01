"""The approved configuration for the first live research test.

SURVEY only, on gpt-5.4-nano, under a one-dollar day and a ten-cent request.
COMPARE and JUDGE are priced but not enabled, because every configured model
fits the ceiling: price is not a permission, so the restriction has to come
from what is built, not from what is affordable.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.research import (  # noqa: E402
    RESEARCH_MAX_INPUT_TOKENS,
    RESEARCH_MAX_OUTPUT_TOKENS,
    build_research_specialist,
)
from alx.config.settings import RuntimeSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    Cognition,
    ModelCompletion,
    ResearchQuestion,
    SpecialistError,
    SpecialistQuestion,
)
from alx.observability.pricing import price_of, worst_case_usd  # noqa: E402
from alx.specialists import ResearchTierModel  # noqa: E402
from alx.specialists.research import ResearchCeilingFailed  # noqa: E402


BASE_ENVIRONMENT = {
    "ALX_REASONING_PROVIDER": "openai",
    "ALX_REASONING_MODEL": "gpt-5.4",
    "OPENAI_API_KEY": "test-key",
    "ALX_STT_PROVIDER": "cartesia",
    "ALX_STT_MODEL": "ink",
    "CARTESIA_API_KEY": "k",
    "ALX_STT_API_VERSION": "2025",
    "ALX_STT_TURN_START_THRESHOLD": "0.7",
    "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.4",
    "ALX_STT_TURN_END_THRESHOLD": "0.1",
    "ALX_STT_TURN_END_TIMEOUT_MS": "1000",
    "ALX_TTS_PROVIDER": "elevenlabs",
    "ALX_TTS_MODEL": "v3",
    "ELEVENLABS_API_KEY": "k",
    "ELEVENLABS_VOICE_ID": "v",
    "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "d",
    "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "1",
    # The approved first-test configuration.
    "ALX_RESEARCH_SURVEY_PROVIDER": "openai",
    "ALX_RESEARCH_SURVEY_MODEL": "gpt-5.4-nano",
    "ALX_RESEARCH_COMPARE_PROVIDER": "openai",
    "ALX_RESEARCH_COMPARE_MODEL": "gpt-5.4-mini",
    "ALX_RESEARCH_JUDGE_PROVIDER": "openai",
    "ALX_RESEARCH_JUDGE_MODEL": "gpt-5.4",
    "ALX_RESEARCH_ENABLED_TIERS": "survey",
    "RESEARCH_DAILY_BUDGET_USD": "1.00",
    "RESEARCH_PER_REQUEST_MAX_USD": "0.10",
}


def environment(**overrides: str) -> dict[str, str]:
    values = dict(BASE_ENVIRONMENT)
    values.update(overrides)
    return values


# What an over-charging provider reports: far more than the enforced bound.
OVERCHARGE_INPUT_TOKENS = 2_000_000
OVERCHARGE_USD = round(
    OVERCHARGE_INPUT_TOKENS / 1e6 * 0.20 + 1_000 / 1e6 * 1.25, 6
)


class OverchargingTransport:
    """A provider that ignores the request bound and bills for far more."""

    supports_bounded_research = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            "openai",
            "gpt-5.4-nano",
            {"finding": "answered"},
            {"input_tokens": OVERCHARGE_INPUT_TOKENS, "output_tokens": 1_000},
        )


def pricing_table() -> dict:
    from alx.observability import pricing

    return dict(pricing.USD_PER_MILLION)


def set_price(key, value) -> None:
    from alx.observability import pricing

    pricing.USD_PER_MILLION[key] = value


def restore_prices(previous: dict) -> None:
    from alx.observability import pricing

    pricing.USD_PER_MILLION.clear()
    pricing.USD_PER_MILLION.update(previous)


def question(tier: Cognition) -> ResearchQuestion:
    return ResearchQuestion(
        SpecialistQuestion(
            "q-1",
            "Answer the question from the material.",
            "Bounded research material.",
            {
                "type": "object",
                "properties": {"finding": {"type": "string"}},
                "required": ["finding"],
                "additionalProperties": False,
            },
        ),
        cognition=tier,
    )


class VerifiedPricingTest(unittest.TestCase):
    """The rates Friedl verified on 2026-09-01, as recorded."""

    def test_the_three_models_carry_their_verified_rates(self) -> None:
        self.assertEqual(price_of("openai", "gpt-5.4-nano"), (0.20, 0.02, 1.25))
        self.assertEqual(price_of("openai", "gpt-5.4-mini"), (0.75, 0.075, 4.50))
        self.assertEqual(price_of("openai", "gpt-5.4"), (2.50, 0.25, 15.00))

    def test_an_unlisted_model_is_still_unpriced(self) -> None:
        """Recording three prices must not imply anything about a fourth."""
        self.assertIsNone(price_of("openai", "gpt-5.4-turbo"))


class SurveyWorstCaseTest(unittest.TestCase):
    def test_one_survey_request_costs_at_most_a_third_of_a_cent(self) -> None:
        worst = worst_case_usd(
            "openai",
            "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS,
            RESEARCH_MAX_OUTPUT_TOKENS,
        )
        # 8,000/1M x 0.20 + 1,000/1M x 1.25 = 0.0016 + 0.00125
        self.assertAlmostEqual(worst, 0.00285, places=6)

    def test_the_worst_case_sits_far_below_the_per_request_ceiling(self) -> None:
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        self.assertLess(worst, 0.10)

    def test_a_full_day_cannot_exceed_the_configured_budget(self) -> None:
        """Even at the worst case every time, the day holds."""
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        affordable = int(1.00 / worst)
        self.assertGreater(affordable, 300)
        self.assertLessEqual(affordable * worst, 1.00)


class EnabledTierTest(unittest.TestCase):
    """COMPARE and JUDGE must be unreachable, not merely expensive."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def build(self, **overrides: str):
        settings = RuntimeSettings.from_environment(environment(**overrides))
        return build_research_specialist(settings.research, self.root)

    def test_survey_is_the_only_tier_built(self) -> None:
        researcher = self.build()
        self.assertIsNotNone(researcher)
        self.assertEqual(set(researcher._tiers), {Cognition.SURVEY})

    def test_compare_is_refused_because_it_was_never_built(self) -> None:
        researcher = self.build()
        with self.assertRaises(SpecialistError) as caught:
            researcher.answer(question(Cognition.COMPARE))
        self.assertIn("cognition_tier_unconfigured", str(caught.exception))

    def test_judge_is_refused_because_it_was_never_built(self) -> None:
        researcher = self.build()
        with self.assertRaises(SpecialistError) as caught:
            researcher.answer(question(Cognition.JUDGE))
        self.assertIn("cognition_tier_unconfigured", str(caught.exception))

    def test_a_refused_tier_reserves_nothing(self) -> None:
        """Refusal happens before the ledger is touched."""
        researcher = self.build()
        before = researcher._ledger.committed_usd()
        with self.assertRaises(SpecialistError):
            researcher.answer(question(Cognition.JUDGE))
        self.assertEqual(researcher._ledger.committed_usd(), before)

    def test_price_alone_would_not_have_stopped_the_other_tiers(self) -> None:
        """Why the restriction is configuration rather than cost.

        All three configured models fit the ten-cent ceiling at this bound, so
        an affordability check would have let COMPARE and JUDGE run.
        """
        for model in ("gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4"):
            worst = worst_case_usd(
                "openai", model,
                RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
            )
            self.assertLess(worst, 0.10, f"{model} would have been affordable")

    def test_research_is_off_when_no_tier_is_enabled(self) -> None:
        self.assertIsNone(self.build(ALX_RESEARCH_ENABLED_TIERS=""))

    def test_research_is_off_when_no_budget_is_configured(self) -> None:
        self.assertIsNone(self.build(RESEARCH_DAILY_BUDGET_USD="0"))

    def test_an_unknown_tier_name_is_refused(self) -> None:
        from alx.config import ConfigurationError

        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_environment(
                environment(ALX_RESEARCH_ENABLED_TIERS="frontier")
            )


class ReservationCoversWorstCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_the_reservation_equals_the_worst_case_of_this_bound(self) -> None:
        settings = RuntimeSettings.from_environment(environment())
        researcher = build_research_specialist(settings.research, self.root)
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        reservation = researcher._ledger.reserve(
            "survey", "openai", "gpt-5.4-nano", worst_case_usd=worst
        )
        self.assertAlmostEqual(reservation.reserved_usd, worst, places=6)
        self.assertAlmostEqual(
            researcher._ledger.committed_usd(), worst, places=6
        )

    def test_a_bound_that_failed_records_the_truth_and_stops_research(self) -> None:
        """If the provider ever exceeds the enforced bound, research halts.

        The ledger records the real charge rather than an accounting clamp, so
        the overrun is visible, and the next call raises rather than spending
        against a ceiling already known to have failed. Recording the true
        figure is what makes the failure inspectable; the halt is what stops it
        compounding.
        """
        settings = RuntimeSettings.from_environment(environment())
        researcher = build_research_specialist(settings.research, self.root)
        reservation = researcher._ledger.reserve(
            "survey", "openai", "gpt-5.4-nano", worst_case_usd=0.00285
        )
        researcher._ledger.settle(reservation, 5.00)
        self.assertGreater(researcher._ledger.overrun_usd(), 0.0)
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question(Cognition.SURVEY))

    def test_no_survey_call_can_reserve_more_than_the_worst_case(self) -> None:
        """What the reservation guarantees while the call is in flight."""
        settings = RuntimeSettings.from_environment(environment())
        researcher = build_research_specialist(settings.research, self.root)
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        for _ in range(10):
            researcher._ledger.reserve(
                "survey", "openai", "gpt-5.4-nano", worst_case_usd=worst
            )
        self.assertAlmostEqual(
            researcher._ledger.committed_usd(), worst * 10, places=6
        )
        self.assertLess(researcher._ledger.committed_usd(), 1.00)


if __name__ == "__main__":
    unittest.main()


class ProviderBoundViolationTest(unittest.TestCase):
    """A charge above the reservation is a safety fault, not a budget event.

    The reservation is the worst case of a bound the provider is supposed to
    enforce. If the measured charge exceeds it, the guarantee has failed rather
    than merely been expensive: the true figure is recorded so telemetry never
    understates spend, and no further paid research may dispatch.
    """

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self._prices = dict(pricing_table())
        # A rate that makes a runaway usage report obviously over the bound.
        set_price(("openai", "gpt-5.4-nano"), (0.20, 0.02, 1.25))

    def tearDown(self) -> None:
        restore_prices(self._prices)
        self._directory.cleanup()

    def researcher(self, transport, root: Path | None = None):
        settings = RuntimeSettings.from_environment(environment())
        built = build_research_specialist(settings.research, root or self.root)
        # Replace only the transport, so the ledger, pricing, bounds and
        # ceiling are exactly the ones the runtime composed.
        built._tiers[Cognition.SURVEY] = ResearchTierModel(
            "openai", "gpt-5.4-nano", transport
        )
        return built

    def test_an_overcharge_is_recorded_truthfully_and_stops_research(self) -> None:
        researcher = self.researcher(OverchargingTransport(), self.root)
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )

        # The overcharging call itself fails. Codex raises on the same call
        # rather than returning its answer and failing the next one, so a
        # result obtained outside the guarantee is never handed back.
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question(Cognition.SURVEY))
        ledger = researcher._ledger

        # 1. The true cost is recorded, not clamped to the reservation.
        call = ledger.day()["calls"][0]
        self.assertGreater(call["actual_usd"], worst)
        self.assertAlmostEqual(call["actual_usd"], OVERCHARGE_USD, places=6)
        self.assertGreater(ledger.committed_usd(), worst)

        # 2. The excess is visible as an overrun and marked as a failure.
        self.assertGreater(ledger.overrun_usd(), 0.0)
        self.assertEqual(call["outcome"], "failed")
        self.assertEqual(call["failure_code"], "cost_overrun")

        # 3. No further paid research can dispatch.
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question(Cognition.SURVEY))

    def test_no_subsequent_call_reaches_the_provider(self) -> None:
        """The halt happens before dispatch, not after another charge."""
        with TemporaryDirectory() as directory:
            transport = OverchargingTransport()
            researcher = self.researcher(transport, Path(directory))
            self._no_further_dispatch(researcher, transport)

    def _no_further_dispatch(self, researcher, transport) -> None:
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question(Cognition.SURVEY))
        self.assertEqual(transport.calls, 1)
        # Every later attempt is refused before the transport is reached.
        for _ in range(3):
            with self.assertRaises(ResearchCeilingFailed):
                researcher.answer(question(Cognition.SURVEY))
        self.assertEqual(transport.calls, 1)


    def test_the_halt_survives_a_restart(self) -> None:
        """A fresh process must not resume spending against a failed ceiling.

        Both researchers share one storage root, which is what a restart is.
        """
        researcher = self.researcher(OverchargingTransport(), self.root)
        with self.assertRaises(ResearchCeilingFailed):
            researcher.answer(question(Cognition.SURVEY))
        reopened = self.researcher(OverchargingTransport(), self.root)
        self.assertGreater(reopened._ledger.overrun_usd(), 0.0)
        with self.assertRaises(ResearchCeilingFailed):
            reopened.answer(question(Cognition.SURVEY))
