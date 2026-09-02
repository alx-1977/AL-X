"""The approved configuration for the first live research test.

SURVEY only, on gpt-5.4-nano, under a one-dollar day and a $0.007 request.
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
    ResearchModelUnpriced,
    ResearchQuestion,
    SpecialistError,
    SpecialistQuestion,
)
from alx.observability.pricing import cost_usd, price_of, worst_case_usd  # noqa: E402
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
    "RESEARCH_PER_REQUEST_MAX_USD": "0.007",
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


class SnapshotTransport:
    """Return the exact canonical snapshot identity observed in the live test."""

    supports_bounded_research = True

    def __init__(self, model: str = "gpt-5.4-nano-2026-03-17") -> None:
        self.model = model
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelCompletion(
            "openai",
            self.model,
            {"finding": "answered"},
            {"input_tokens": 189, "output_tokens": 1_045},
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


def _usage(cache_write: int = 0) -> dict[str, int]:
    """One canonical, fully measured usage report."""
    return {
        "input_tokens": 10_000,
        "cached_tokens": 0,
        "output_tokens": 1_000,
        "reasoning_tokens": 0,
        "cache_write_tokens": cache_write,
    }


class VerifiedPricingTest(unittest.TestCase):
    """The rates Friedl verified on 2026-09-01, as recorded."""

    def test_the_three_models_carry_their_verified_rates(self) -> None:
        self.assertEqual(price_of("openai", "gpt-5.4-nano"), (0.20, 0.02, 1.25, None))
        self.assertEqual(price_of("openai", "gpt-5.4-mini"), (0.75, 0.075, 4.50, None))
        self.assertEqual(price_of("openai", "gpt-5.4"), (2.50, 0.25, 15.00, None))

    def test_the_gpt_5_4_generation_carries_no_inferred_cache_write_rate(self) -> None:
        """A GPT-5.6 fact must not become a GPT-5.4 rate.

        Cache-write billing was verified for gpt-5.6-sol only. Deriving a rate
        for an earlier generation from that multiplier would be exactly the
        cross-family inference this table refuses, so these stay None until an
        authoritative rate is recorded for them.
        """
        for model in ("gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4"):
            with self.subTest(model=model):
                self.assertIsNone(price_of("openai", model).cache_write)

    def test_the_core_model_carries_its_verified_rates(self) -> None:
        """Recorded by Friedl on 2026-09-02 for the D-005 Core."""
        self.assertEqual(
            price_of("openai", "gpt-5.6-sol"), (4.00, 0.40, 20.00, 5.00)
        )

    def test_the_autonomous_experiment_model_carries_its_verified_rates(self) -> None:
        """Recorded by Friedl on 2026-09-02 for the Luna autonomous evaluation.

        Same generation as Sol, so cache writes bill separately. The rate is
        verified for this model rather than derived from Sol's, even though the
        1.25x relationship happens to hold for both.
        """
        self.assertEqual(
            price_of("openai", "gpt-5.6-luna"), (0.20, 0.02, 1.20, 0.25)
        )

    def test_an_unapproved_experiment_snapshot_cannot_inherit_pricing(self) -> None:
        self.assertIsNone(price_of("openai", "gpt-5.6-luna-preview"))
        self.assertIsNone(price_of("openai", "gpt-5.6-luna-2026-09-02"))

    def test_an_unapproved_core_snapshot_cannot_inherit_core_pricing(self) -> None:
        """Exact identity only, for the Core model as for every other."""
        self.assertIsNone(price_of("openai", "gpt-5.6-sol-2026-09-02"))
        self.assertIsNone(price_of("openai", "gpt-5.6"))
        self.assertIsNone(price_of("openai", "gpt-5.6-sol-preview"))

    def test_cache_writes_are_charged_when_the_model_bills_them(self) -> None:
        """The Core writes its stable prefix on every call; that is a real cost.

        Leaving writes unpriced made a cache-heavy call look cheaper than it
        was, against a hard daily ceiling.
        """
        without = cost_usd("openai", "gpt-5.6-sol", _usage(cache_write=0))
        with_writes = cost_usd("openai", "gpt-5.6-sol", _usage(cache_write=10_000))
        self.assertGreater(with_writes, without)
        # 10,000 tokens at $5.00 per million.
        self.assertAlmostEqual(with_writes - without, 0.05, places=6)

    def test_cache_writes_cost_nothing_when_the_model_does_not_bill_them(self) -> None:
        """A None rate means not applicable, so reported writes add nothing."""
        without = cost_usd("openai", "gpt-5.4", _usage(cache_write=0))
        with_writes = cost_usd("openai", "gpt-5.4", _usage(cache_write=10_000))
        self.assertEqual(with_writes, without)

    def test_the_worst_case_charges_input_once_as_uncached_and_once_as_write(
        self,
    ) -> None:
        """A cache miss that also writes every input token is the expensive case."""
        worst = worst_case_usd("openai", "gpt-5.6-sol", 32_000, 8_000)
        # 32,000 x ($4.00 + $5.00) + 8,000 x $20.00, per million.
        self.assertAlmostEqual(worst, 0.288 + 0.160, places=6)

    def test_a_model_without_a_write_rate_has_no_write_term_in_its_worst_case(
        self,
    ) -> None:
        worst = worst_case_usd("openai", "gpt-5.4", 32_000, 8_000)
        self.assertAlmostEqual(worst, 32_000 / 1e6 * 2.50 + 8_000 / 1e6 * 15.00, 6)

    def test_an_unlisted_model_is_still_unpriced(self) -> None:
        """Recording three prices must not imply anything about a fourth."""
        self.assertIsNone(price_of("openai", "gpt-5.4-turbo"))

    def test_the_approved_snapshot_inherits_the_verified_alias_price(self) -> None:
        self.assertEqual(
            price_of("openai", "gpt-5.4-nano-2026-03-17"),
            price_of("openai", "gpt-5.4-nano"),
        )

    def test_a_plausible_but_unapproved_snapshot_cannot_inherit_pricing(self) -> None:
        self.assertIsNone(price_of("openai", "gpt-5.4-nano-2026-03-18"))
        self.assertIsNone(price_of("openai", "gpt-5.4-nano-preview"))


class SnapshotIdentitySettlementTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def researcher(self, transport, telemetry=None):
        settings = RuntimeSettings.from_environment(environment())
        built = build_research_specialist(
            settings.research, self.root, telemetry_sink=telemetry
        )
        built._tiers[Cognition.SURVEY] = ResearchTierModel(
            "openai", "gpt-5.4-nano", transport
        )
        return built

    def test_snapshot_is_priced_while_telemetry_preserves_actual_identity(self) -> None:
        events: list[dict] = []
        researcher = self.researcher(
            SnapshotTransport(), lambda _task_id, values: events.append(dict(values))
        )

        self.assertEqual(
            researcher.answer(question(Cognition.SURVEY)), {"finding": "answered"}
        )
        call = researcher._ledger.day()["calls"][0]
        self.assertEqual(call["model"], "gpt-5.4-nano-2026-03-17")
        self.assertEqual(call["outcome"], "succeeded")
        self.assertEqual(call["failure_code"], "")
        completed = [event for event in events if event["code"] == "research.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["model"], "gpt-5.4-nano-2026-03-17")

    def test_unapproved_snapshot_still_fails_as_actual_model_unpriced(self) -> None:
        researcher = self.researcher(
            SnapshotTransport("gpt-5.4-nano-2026-03-18")
        )
        with self.assertRaises(ResearchModelUnpriced):
            researcher.answer(question(Cognition.SURVEY))
        call = researcher._ledger.day()["calls"][0]
        self.assertEqual(call["model"], "gpt-5.4-nano-2026-03-18")
        self.assertEqual(call["outcome"], "failed")
        self.assertEqual(call["failure_code"], "actual_model_unpriced")


class SurveyWorstCaseTest(unittest.TestCase):
    def test_one_survey_request_costs_at_most_sixty_six_hundredths_of_a_cent(self) -> None:
        worst = worst_case_usd(
            "openai",
            "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS,
            RESEARCH_MAX_OUTPUT_TOKENS,
        )
        # 8,000/1M x 0.20 + 4,000/1M x 1.25 = 0.0016 + 0.005
        self.assertAlmostEqual(worst, 0.0066, places=6)

    def test_the_worst_case_sits_far_below_the_per_request_ceiling(self) -> None:
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        settings = RuntimeSettings.from_environment(environment())
        self.assertLess(worst, settings.research.limits.per_request_max_usd)
        self.assertEqual(settings.research.limits.per_request_max_usd, 0.007)

    def test_a_full_day_cannot_exceed_the_configured_budget(self) -> None:
        """Even at the worst case every time, the day holds."""
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        affordable = int(1.00 / worst)
        self.assertGreater(affordable, 150)
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

    def test_other_tiers_remain_unbuilt_even_with_a_larger_ceiling(self) -> None:
        """Enabled tiers, not incidental affordability, grant access."""
        researcher = self.build(RESEARCH_PER_REQUEST_MAX_USD="0.10")
        self.assertEqual(set(researcher._tiers), {Cognition.SURVEY})

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
        worst = worst_case_usd(
            "openai", "gpt-5.4-nano",
            RESEARCH_MAX_INPUT_TOKENS, RESEARCH_MAX_OUTPUT_TOKENS,
        )
        reservation = researcher._ledger.reserve(
            "survey", "openai", "gpt-5.4-nano", worst_case_usd=worst
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


class AuthoritativeRuntimePathTest(unittest.TestCase):
    """Research is reached through the broker, or not at all."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self._call_id = [""]

    def tearDown(self) -> None:
        self._directory.cleanup()

    def runtime(self, **overrides: str):
        from alx.bootstrap.research import build_research_runtime

        settings = RuntimeSettings.from_environment(environment(**overrides))
        return build_research_runtime(
            settings.research, self.root, lambda: self._call_id[0]
        )

    def broker(self, runtime):
        from alx.capabilities import CapabilityBroker, CapabilityRegistry
        from alx.safety import SafetyGate

        registry = CapabilityRegistry()
        for definition in runtime.definitions:
            registry.register(definition)
        return registry, CapabilityBroker(
            registry, SafetyGate(dict(runtime.policies)), dict(runtime.executors)
        )

    def authority(self, permissions):
        from datetime import UTC, datetime

        from alx.safety import AuthorityContext

        return AuthorityContext(
            "friedl", frozenset(permissions), datetime.now(UTC)
        )

    def test_research_is_one_capability_in_the_one_catalogue(self) -> None:
        runtime = self.runtime()
        registry, _broker = self.broker(runtime)
        self.assertEqual(
            [d.capability_id for d in registry.list_definitions()],
            ["ask_research_question"],
        )

    def test_spending_requires_its_own_permission(self) -> None:
        from alx.contracts import CapabilityAttemptDisposition, CapabilityCall

        runtime = self.runtime()
        _registry, broker = self.broker(runtime)
        self._call_id[0] = "call-1"
        attempt = broker.dispatch(
            CapabilityCall(
                "call-1",
                "ask_research_question",
                {"question_id": "q", "instruction": "i", "material": "m"},
            ),
            self.authority(set()),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "permission_missing")
        self.assertFalse(attempt.implementation_invoked)

    def test_an_unenabled_tier_is_refused_through_the_broker(self) -> None:
        from alx.contracts import CapabilityCall, CapabilityResultState

        runtime = self.runtime()
        _registry, broker = self.broker(runtime)
        for tier in ("compare", "judge"):
            self._call_id[0] = f"call-{tier}"
            attempt = broker.dispatch(
                CapabilityCall(
                    f"call-{tier}",
                    "ask_research_question",
                    {
                        "question_id": "q",
                        "instruction": "i",
                        "material": "m",
                        "cognition": tier,
                    },
                ),
                self.authority(runtime.permissions),
            )
            self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
            self.assertEqual(
                attempt.result.failure["code"], "cognition_tier_unconfigured"
            )

    def test_research_is_absent_when_no_tier_is_enabled(self) -> None:
        """Unregistered, so AL/X cannot even propose the call."""
        self.assertIsNone(self.runtime(ALX_RESEARCH_ENABLED_TIERS=""))

    def test_there_is_one_research_execution_path(self) -> None:
        """No second entry point, and no improvised direct invocation."""
        callers = [
            path.name
            for path in (REPOSITORY_ROOT / "src" / "alx").rglob("*.py")
            if ".answer(" in path.read_text()
            and "ResearchQuestion" in path.read_text()
        ]
        self.assertEqual(callers, ["research.py"])

    def test_the_runtime_registers_research_through_the_shared_broker(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"
        ).read_text()
        self.assertIn("build_research_runtime(", source)
        self.assertIn("registry.register(definition)", source)
        # No scheduler, no recurring research, no background loop.
        for forbidden in ("create_task", "call_later", "Timer(", "while True"):
            self.assertNotIn(forbidden, source)
