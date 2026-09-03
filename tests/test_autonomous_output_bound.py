"""An autonomous Core request carries a finite provider-side output ceiling.

Conversation must not carry one: an answer to Friedl truncated mid-sentence by
an arbitrary bound is a worse failure than an expensive one. Anything spending
against a dollar ceiling must carry one, because without a finite bound there
is no worst-case price and no reservation can be honest.

The bound limits what the provider generates. These tests pin that it changes
nothing else, so it can never quietly become a quality setting that makes the
autonomous Core a different mind.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ReasoningContext  # noqa: E402
from alx.core.model_reasoner import ModelReasoner  # noqa: E402


class CapturingModel:
    """Records the request and stops before any provider work."""

    def __init__(self) -> None:
        self.request = None

    def complete(self, request):
        self.request = request
        raise RuntimeError("captured")


def _request(max_output_tokens: int | None):
    model = CapturingModel()
    reasoner = ModelReasoner(model, "laws", "identity", max_output_tokens)
    try:
        reasoner.decide(ReasoningContext(None, (), (), conversation_id="c1"))
    except Exception:
        pass
    return model.request


class AutonomousOutputBoundTests(unittest.TestCase):
    def test_an_autonomous_reasoner_bounds_its_output(self) -> None:
        self.assertEqual(_request(32_000).max_output_tokens, 32_000)

    def test_the_conversational_reasoner_carries_no_bound(self) -> None:
        """The user-initiated Sol path must stay exactly as it was."""
        self.assertIsNone(_request(None).max_output_tokens)

    def test_the_bound_changes_nothing_else_about_the_request(self) -> None:
        """Same mind, same prompt, same everything but the generation ceiling."""
        unbounded, bounded = _request(None), _request(32_000)
        self.assertEqual(
            [item.content for item in unbounded.messages],
            [item.content for item in bounded.messages],
        )
        self.assertEqual(
            [item.role for item in unbounded.messages],
            [item.role for item in bounded.messages],
        )
        for field in (
            "output_schema_name", "output_schema", "affinity_key",
            "cache_key", "kind", "tier", "reservation_id", "reserved_usd",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(unbounded, field), getattr(bounded, field)
                )

    def test_both_reasoners_receive_the_same_laws_and_identity(self) -> None:
        """A bounded Core is the same AL/X, not a different one."""
        unbounded = ModelReasoner(CapturingModel(), "laws", "identity", None)
        bounded = ModelReasoner(CapturingModel(), "laws", "identity", 32_000)
        self.assertEqual(
            unbounded._constitutional_context, bounded._constitutional_context
        )

    def test_a_non_positive_bound_is_refused(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ModelReasoner(CapturingModel(), "laws", "identity", value)

    def test_the_bound_is_fixed_at_construction_not_chosen_per_turn(self) -> None:
        """A per-turn choice would be a decision; a fixed rail is not.

        The reasoner exposes no way for a caller, a context or a decision to
        vary the ceiling, so nothing can select a bound from what AL/X is
        thinking about.
        """
        reasoner = ModelReasoner(CapturingModel(), "laws", "identity", 32_000)
        self.assertNotIn(
            "max_output_tokens",
            ReasoningContext(None, (), (), conversation_id="c1").__annotations__,
        )
        self.assertEqual(reasoner._max_output_tokens, 32_000)


if __name__ == "__main__":
    unittest.main()
