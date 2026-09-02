"""Provider usage is flattened once, and both consumers read the same numbers.

Telemetry flattened OpenAI's nested usage while cost settlement read the raw
response, so a real reply priced its cached tokens at the uncached rate and
missed its reasoning tokens entirely. These tests use the shapes providers
actually return, because synthetic flat usage is exactly what hid the defect.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

import httpx  # noqa: E402

from alx.contracts import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelRole,
    is_measured,
    normalise_usage,
)
from alx.observability import pricing  # noqa: E402
from alx.providers import OpenAIReasoningModel, XAIReasoningModel  # noqa: E402


# The shape the OpenAI Responses API actually returns.
OPENAI_USAGE = {
    "input_tokens": 10_000,
    "output_tokens": 2_000,
    "total_tokens": 12_000,
    "input_tokens_details": {"cached_tokens": 8_000, "cache_write_tokens": 512},
    "output_tokens_details": {"reasoning_tokens": 1_500},
}

# The shape an OpenAI-style chat completions endpoint returns.
CHAT_USAGE = {
    "prompt_tokens": 10_000,
    "completion_tokens": 2_000,
    "total_tokens": 12_000,
    "prompt_tokens_details": {"cached_tokens": 8_000},
    "completion_tokens_details": {"reasoning_tokens": 1_500},
}

SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


def request() -> ModelRequest:
    # An affinity key is what routes telemetry to a task, so it is required for
    # the sink to fire at all.
    return ModelRequest(
        (ModelMessage(ModelRole.USER, "question"),),
        "answer",
        SCHEMA,
        affinity_key="task-1",
        max_output_tokens=1_000,
    )


class NormalisationTest(unittest.TestCase):
    def test_the_openai_response_shape_flattens_correctly(self) -> None:
        values = normalise_usage(OPENAI_USAGE)
        self.assertEqual(values["input_tokens"], 10_000)
        self.assertEqual(values["cached_tokens"], 8_000)
        self.assertEqual(values["cache_write_tokens"], 512)
        self.assertEqual(values["output_tokens"], 2_000)
        self.assertEqual(values["reasoning_tokens"], 1_500)

    def test_the_chat_completions_shape_flattens_to_the_same_names(self) -> None:
        values = normalise_usage(CHAT_USAGE)
        self.assertEqual(values["input_tokens"], 10_000)
        self.assertEqual(values["cached_tokens"], 8_000)
        self.assertEqual(values["output_tokens"], 2_000)
        self.assertEqual(values["reasoning_tokens"], 1_500)

    def test_cached_tokens_cannot_exceed_input_tokens(self) -> None:
        """A nonsense report must not produce a negative uncached count."""
        values = normalise_usage(
            {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 900}}
        )
        self.assertEqual(values["cached_tokens"], 100)

    def test_a_missing_report_normalises_to_zeros_and_is_unmeasured(self) -> None:
        for absent in (None, {}, "usage", 42, []):
            values = normalise_usage(absent)
            self.assertEqual(values["input_tokens"], 0)
            self.assertFalse(is_measured(values))


class CachedPricingTest(unittest.TestCase):
    """Cached input must be billed at the cached rate, not the full one."""

    def setUp(self) -> None:
        self._prices = dict(pricing.USD_PER_MILLION)

    def tearDown(self) -> None:
        pricing.USD_PER_MILLION.clear()
        pricing.USD_PER_MILLION.update(self._prices)

    def test_cached_tokens_receive_the_cached_rate(self) -> None:
        values = normalise_usage(OPENAI_USAGE)
        cost = pricing.cost_usd("openai", "gpt-5.4-nano", values)
        # 2,000 uncached x 0.20 + 8,000 cached x 0.02 + 3,500 out x 1.25
        expected = round(
            2_000 / 1e6 * 0.20 + 8_000 / 1e6 * 0.02 + 3_500 / 1e6 * 1.25, 6
        )
        self.assertAlmostEqual(cost, expected, places=6)

    def test_pricing_the_raw_response_would_have_undercharged(self) -> None:
        """Why normalisation is the fix, stated as a measurement.

        The raw response hides cached tokens under a nested key, so every input
        token looks uncached and every reasoning token vanishes.
        """
        normalised = pricing.cost_usd(
            "openai", "gpt-5.4-nano", normalise_usage(OPENAI_USAGE)
        )
        raw = pricing.cost_usd("openai", "gpt-5.4-nano", OPENAI_USAGE)
        self.assertNotAlmostEqual(normalised, raw, places=6)

    def test_reasoning_tokens_are_billed_as_output(self) -> None:
        without = normalise_usage(
            {"input_tokens": 0, "output_tokens": 2_000}
        )
        with_reasoning = normalise_usage(
            {
                "input_tokens": 0,
                "output_tokens": 2_000,
                "output_tokens_details": {"reasoning_tokens": 1_500},
            }
        )
        cheaper = pricing.cost_usd("openai", "gpt-5.4-nano", without)
        dearer = pricing.cost_usd("openai", "gpt-5.4-nano", with_reasoning)
        self.assertAlmostEqual(dearer - cheaper, 1_500 / 1e6 * 1.25, places=6)

    def test_an_unmeasured_report_is_never_priced_as_free(self) -> None:
        self.assertIsNone(
            pricing.cost_usd("openai", "gpt-5.4-nano", normalise_usage({}))
        )
        self.assertIsNone(
            pricing.cost_usd("openai", "gpt-5.4-nano", normalise_usage(None))
        )

    def test_the_measured_field_mirror_has_not_drifted(self) -> None:
        """Observability may not import contracts, so the list is duplicated."""
        from alx.contracts.usage import MEASURED_FIELDS as canonical

        self.assertEqual(pricing.MEASURED_FIELDS, canonical)


class AdapterNormalisationTest(unittest.TestCase):
    """The adapter is the one boundary where flattening happens."""

    def _openai(self, usage: dict) -> tuple:
        captured: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "gpt-5.4-nano",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text",
                                 "text": json.dumps({"finding": "answered"})}
                            ],
                        }
                    ],
                    "usage": usage,
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        model = OpenAIReasoningModel(
            "gpt-5.4-nano", "key", "https://api.openai.com", 30,
            client=client, streaming=False,
            telemetry_sink=lambda task, values: captured.append(values),
        )
        return model.complete(request()), captured

    def test_the_completion_carries_canonical_usage(self) -> None:
        completion, _ = self._openai(OPENAI_USAGE)
        self.assertEqual(completion.usage["cached_tokens"], 8_000)
        self.assertEqual(completion.usage["reasoning_tokens"], 1_500)
        # The provider's own nested keys do not survive the boundary.
        self.assertNotIn("input_tokens_details", completion.usage)

    def test_telemetry_and_settlement_read_the_same_numbers(self) -> None:
        completion, captured = self._openai(OPENAI_USAGE)
        self.assertTrue(captured)
        event = captured[-1]
        for field in ("input_tokens", "cached_tokens", "output_tokens",
                      "reasoning_tokens"):
            self.assertEqual(
                event[field], completion.usage[field],
                f"telemetry and settlement disagree on {field}",
            )

    def test_provider_telemetry_is_not_overwritten_with_zeros(self) -> None:
        _completion, captured = self._openai(OPENAI_USAGE)
        event = captured[-1]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-5.4-nano")
        self.assertGreater(event["input_tokens"], 0)
        self.assertGreater(event["cached_tokens"], 0)
        self.assertGreater(event["reasoning_tokens"], 0)

    def test_a_response_without_usage_yields_an_unmeasured_report(self) -> None:
        completion, _ = self._openai({})
        self.assertFalse(is_measured(completion.usage))
        self.assertIsNone(
            pricing.cost_usd("openai", "gpt-5.4-nano", completion.usage)
        )

    def test_only_the_adapters_parse_a_provider_usage_layout(self) -> None:
        """One boundary: nothing else may read a vendor's own field names."""
        vendor_keys = (
            "input_tokens_details", "output_tokens_details",
            "prompt_tokens_details", "completion_tokens_details",
            "prompt_tokens", "completion_tokens",
        )
        allowed = {"usage.py"}
        for path in (REPOSITORY_ROOT / "src" / "alx").rglob("*.py"):
            if path.name in allowed:
                continue
            source = path.read_text()
            for key in vendor_keys:
                self.assertNotIn(
                    key, source,
                    f"{path.name} parses provider usage outside the boundary",
                )


if __name__ == "__main__":
    unittest.main()
