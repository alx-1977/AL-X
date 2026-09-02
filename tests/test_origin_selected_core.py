"""Two Cores, chosen by provenance alone — the D-024a experiment.

Recorded as time-boxed, not architecture. These tests exist to keep it that
way: they pin that the choice reads nothing but where the turn came from, that
both Cores are the same AL/X over different models, and that nothing
downstream can discover which one answered.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.reasoning import OriginSelectedReasoner  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, CognitionOrigin, ReasoningContext,
)


class RecordingReasoner:
    def __init__(self, name: str) -> None:
        self.name = name
        self.contexts: list[ReasoningContext] = []

    def decide(self, context: ReasoningContext) -> AgentDecision:
        self.contexts.append(context)
        return AgentDecision(response=self.name)


def _context(origin: CognitionOrigin) -> ReasoningContext:
    return ReasoningContext(None, (), (), conversation_id="c1", origin=origin)


class OriginSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sol = RecordingReasoner("sol")
        self.luna = RecordingReasoner("luna")
        self.reasoner = OriginSelectedReasoner(self.sol, self.luna)

    def test_a_person_turn_is_answered_by_the_conversational_core(self) -> None:
        outcome = self.reasoner.decide(_context(CognitionOrigin.PERSON_TURN))
        self.assertEqual(outcome.response, "sol")
        self.assertEqual(len(self.luna.contexts), 0)

    def test_every_autonomous_origin_is_answered_by_the_autonomous_core(self) -> None:
        for origin in (
            CognitionOrigin.SELF_REQUESTED,
            CognitionOrigin.EXTERNAL_EVENT,
            CognitionOrigin.WORK_COMPLETED,
        ):
            with self.subTest(origin=origin):
                self.assertEqual(self.reasoner.decide(_context(origin)).response, "luna")

    def test_the_context_reaching_both_cores_is_identical(self) -> None:
        """Same continuity context shape, whichever model answers."""
        person = _context(CognitionOrigin.PERSON_TURN)
        autonomous = _context(CognitionOrigin.SELF_REQUESTED)
        self.reasoner.decide(person)
        self.reasoner.decide(autonomous)
        seen_sol, seen_luna = self.sol.contexts[0], self.luna.contexts[0]
        for field in (
            "active_goal", "turns", "capabilities", "memories", "events",
            "transient_attempts", "conversation_id", "unfinished_goals",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(seen_sol, field), getattr(seen_luna, field)
                )
        # Origin is the one field that differs, and it is provenance only.
        self.assertNotEqual(seen_sol.origin, seen_luna.origin)

    def test_selection_consults_nothing_but_the_origin(self) -> None:
        """A context rich in goals, memories and events must not sway it."""
        from alx.contracts import BackgroundEvent, MemoryKind
        from datetime import UTC, datetime

        event = BackgroundEvent("e1", "mail.arrived", datetime(2026, 9, 2, tzinfo=UTC))
        rich = ReasoningContext(
            None, (), (), events=(event,), conversation_id="c1",
            origin=CognitionOrigin.PERSON_TURN,
        )
        self.assertEqual(self.reasoner.decide(rich).response, "sol")
        bare = ReasoningContext(
            None, (), (), conversation_id="c1",
            origin=CognitionOrigin.SELF_REQUESTED,
        )
        self.assertEqual(self.reasoner.decide(bare).response, "luna")


class NoSecondSelectionSiteTests(unittest.TestCase):
    """Law 0: one selection, in composition, and nowhere else."""

    SOURCE = Path(__file__).resolve().parents[1] / "src" / "alx"

    def test_only_composition_references_the_experimental_selection(self) -> None:
        allowed = {"bootstrap/reasoning.py", "bootstrap/live_voice.py"}
        offenders = []
        for path in sorted(self.SOURCE.rglob("*.py")):
            relative = path.relative_to(self.SOURCE).as_posix()
            if relative in allowed:
                continue
            if "OriginSelectedReasoner" in path.read_text(encoding="utf-8"):
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_the_core_never_learns_which_model_answered(self) -> None:
        """CoreAgent, the broker and the gate must not be able to find out.

        They may know a turn's provenance, because origin is threaded through
        for the context. They may not know, or be able to discover, which model
        produced the decision.
        """
        for module in ("core/loop.py", "capabilities/broker.py", "safety/gate.py"):
            with self.subTest(module=module):
                source = (self.SOURCE / module).read_text(encoding="utf-8")
                for token in (
                    "gpt-5.6-luna", "gpt-5.6-sol", "OriginSelectedReasoner",
                    "autonomous_model", "_conversational", "_autonomous",
                ):
                    self.assertNotIn(token, source)

    def test_the_decision_contract_carries_no_model_identity(self) -> None:
        self.assertNotIn("model", AgentDecision.__dataclass_fields__)


class ArchitectureGateTests(unittest.TestCase):
    """The gate must reject a selection that reads meaning."""

    def _violations(self, source: str) -> list[str]:
        import ast
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from check_architecture import _autonomous_selection_violations

        tree = ast.parse(source)
        return [
            item.message
            for item in _autonomous_selection_violations(
                "src/alx/bootstrap/reasoning.py", tree
            )
        ]

    def test_an_origin_only_selection_passes(self) -> None:
        source = (
            "class OriginSelectedReasoner:\n"
            "    def decide(self, context):\n"
            "        return context.origin.is_autonomous\n"
        )
        self.assertEqual(self._violations(source), [])

    def test_a_selection_reading_semantic_state_is_rejected(self) -> None:
        for attribute in ("topic", "goals", "importance", "capabilities", "keywords"):
            with self.subTest(attribute=attribute):
                source = (
                    "class OriginSelectedReasoner:\n"
                    "    def decide(self, context):\n"
                    f"        return context.{attribute}\n"
                )
                self.assertTrue(
                    any("select on meaning" in item for item in self._violations(source))
                )

    def test_the_selection_may_not_live_outside_its_module(self) -> None:
        import ast
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from check_architecture import _autonomous_selection_violations

        tree = ast.parse(
            "class OriginSelectedReasoner:\n"
            "    def decide(self, context):\n"
            "        return context.origin.is_autonomous\n"
        )
        messages = [
            item.message
            for item in _autonomous_selection_violations("src/alx/core/loop.py", tree)
        ]
        self.assertTrue(any("may live only in" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
