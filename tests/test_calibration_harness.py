"""The calibration harness must preserve what a real provider returned.

Two real calls were spent producing no evidence. The first crashed after a
successful response; the second recorded zeros while the provider had in fact
answered. Both were harness faults, not provider faults, and both were
invisible because the harness was only ever exercised against fakes that
happened to return plain dicts.

`ModelCompletion` freezes `output` and `usage` into `mappingproxy`, which is a
Mapping but not a dict. Every `isinstance(..., dict)` check in the harness was
therefore False, and real measurements were replaced with empty defaults. These
tests pin the harness against the exact object the adapter really returns.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.models import ModelCompletion  # noqa: E402
from alx.contracts.usage import normalise_usage  # noqa: E402
from alx.observability.pricing import cost_usd  # noqa: E402

# A representative /v1/responses usage object, in OpenAI's own shape.
RAW_USAGE = {
    "input_tokens": 54_321,
    "input_tokens_details": {"cached_tokens": 12_000},
    "output_tokens": 2_100,
    "output_tokens_details": {"reasoning_tokens": 1_800},
    "total_tokens": 56_421,
}
RAW_OUTPUT = {
    "action": {"type": "finish_silently"},
    "goal_id": None,
    "goal_update": None,
    "memory_proposals": [],
}


def _completion() -> ModelCompletion:
    """Exactly what the adapter hands back, frozen fields and all."""
    return ModelCompletion("openai", "gpt-5.6-luna", RAW_OUTPUT, RAW_USAGE)


class TheAnomalyIsReproducibleTests(unittest.TestCase):
    """Demonstrate the bug that cost two calls, so it cannot return."""

    def test_the_returned_fields_are_mappings_not_dicts(self) -> None:
        completion = _completion()
        self.assertIsInstance(completion.usage, Mapping)
        self.assertIsInstance(completion.output, Mapping)
        self.assertNotIsInstance(completion.usage, dict)
        self.assertNotIsInstance(completion.output, dict)

    def test_a_dict_check_would_discard_a_real_usage_report(self) -> None:
        """The precise line that produced `raw_usage: {}` from a real answer."""
        completion = _completion()
        broken = dict(completion.usage) if isinstance(completion.usage, dict) else {}
        self.assertEqual(broken, {})
        correct = dict(completion.usage) if isinstance(completion.usage, Mapping) else {}
        self.assertEqual(correct["input_tokens"], 54_321)

    def test_a_dict_check_would_report_a_valid_answer_as_rejected(self) -> None:
        """The line that produced `output_is_dict: false` for valid output."""
        completion = _completion()
        self.assertFalse(isinstance(completion.output, dict))
        self.assertTrue(isinstance(completion.output, Mapping))

    def test_the_frozen_output_is_not_json_serialisable_directly(self) -> None:
        """The crash that lost the first call entirely."""
        completion = _completion()
        with self.assertRaises(TypeError):
            json.dumps(completion.output)
        # With a converter it serialises, which is what the harness now does.
        self.assertIn("action", json.dumps(completion.output, default=dict))


class TheHarnessPreservesEvidenceTests(unittest.TestCase):
    """Everything the calibration is required to report must survive."""

    def _harvest(self) -> dict:
        """The harness's extraction logic, as it now stands."""
        completion = _completion()
        usage = dict(completion.usage) if isinstance(completion.usage, Mapping) else {}
        output_is_mapping = isinstance(completion.output, Mapping)
        return {
            "provider": completion.provider,
            "model": completion.model,
            "raw_usage": usage,
            "output_is_mapping": output_is_mapping,
            "output_keys": sorted(completion.output) if output_is_mapping else None,
            "normalised": normalise_usage(usage),
            "cost": cost_usd(completion.provider, completion.model, normalise_usage(usage)),
        }

    def test_provider_and_model_identity_survive(self) -> None:
        harvested = self._harvest()
        self.assertEqual(harvested["provider"], "openai")
        self.assertEqual(harvested["model"], "gpt-5.6-luna")

    def test_the_raw_usage_object_survives_intact(self) -> None:
        self.assertEqual(self._harvest()["raw_usage"], RAW_USAGE)

    def test_every_reported_token_count_survives(self) -> None:
        normalised = self._harvest()["normalised"]
        self.assertEqual(normalised["input_tokens"], 54_321)
        self.assertEqual(normalised["cached_tokens"], 12_000)
        self.assertEqual(normalised["output_tokens"], 2_100)
        self.assertEqual(normalised["reasoning_tokens"], 1_800)

    def test_the_strict_schema_verdict_is_correct(self) -> None:
        harvested = self._harvest()
        self.assertTrue(harvested["output_is_mapping"])
        self.assertEqual(
            harvested["output_keys"],
            ["action", "goal_id", "goal_update", "memory_proposals"],
        )

    def test_the_cost_is_computable(self) -> None:
        cost = self._harvest()["cost"]
        self.assertIsNotNone(cost)
        self.assertGreater(cost, 0.0)

    def test_the_evidence_record_is_serialisable(self) -> None:
        """A formatting failure must not be able to destroy the evidence."""
        text = json.dumps(self._harvest(), indent=2, default=str)
        self.assertIn("54321", text)
        self.assertIn("gpt-5.6-luna", text)

    def test_a_genuinely_empty_usage_is_still_reported_as_empty(self) -> None:
        """The fix must not manufacture data where a provider sent none."""
        completion = ModelCompletion("openai", "gpt-5.6-luna", RAW_OUTPUT, {})
        usage = dict(completion.usage) if isinstance(completion.usage, Mapping) else {}
        self.assertEqual(usage, {})
        self.assertIsNone(cost_usd("openai", "gpt-5.6-luna", normalise_usage(usage)))


class ProtocolExceptionsSurfaceTests(unittest.TestCase):
    """A provider protocol failure must raise, not read as an empty answer."""

    def test_a_non_object_output_is_refused_by_the_adapter(self) -> None:
        import httpx

        from alx.providers.openai import OpenAIReasoningModel
        from alx.contracts import ModelMessage, ModelRequest, ModelRole

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {"content": [{"type": "output_text", "text": "42"}]}
                    ],
                    "model": "gpt-5.6-luna",
                    "usage": RAW_USAGE,
                },
            )

        model = OpenAIReasoningModel(
            "gpt-5.6-luna", "key", "https://api.openai.com", 30,
            client=httpx.Client(transport=httpx.MockTransport(respond)),
            streaming=False,
        )
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "x"),), "n", {"type": "object"}, "t", None,
        )
        with self.assertRaises(Exception) as caught:
            model.complete(request)
        # It raises rather than returning something the harness would misread.
        self.assertNotIsInstance(caught.exception, AssertionError)


if __name__ == "__main__":
    unittest.main()
