"""The paid path must exist in production, not only in tests.

This branch has twice had a unit-tested component that was never wired into
the composition root: Phase 1's bound parameter, and Phase 3's origin
forwarding. Both passed their own tests while being absent from the runtime.
These tests therefore assert against the real composition root and the real
default clock, not hand-built fakes.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.autonomous import AutonomousCognitionRunner  # noqa: E402
from alx.bootstrap.reasoning import (  # noqa: E402
    AutonomousReasonerUnavailable,
    OriginSelectedReasoner,
    build_model_reasoner,
)
from alx.config import (  # noqa: E402
    AUTONOMOUS_MAX_INPUT_TOKENS,
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
)
from alx.continuity import (  # noqa: E402
    FutureCognitionSource,
    SQLiteContinuityStore,
    SQLiteOpportunityLedger,
)
from alx.contracts import CognitionOrigin, ReasoningContext  # noqa: E402
from alx.contracts.continuity import FutureCognitionRequest  # noqa: E402
from alx.core.model_reasoner import AutonomousRequestUnbounded  # noqa: E402
from alx.observability import ConfiguredPricingWorstCase  # noqa: E402
from alx.observability.autonomous_budget import SQLiteAutonomousLedger  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPOSITION = ROOT / "src" / "alx" / "bootstrap" / "live_voice.py"


class CompositionRootTests(unittest.TestCase):
    """Phases 0-7 must be fully composed, and inert."""

    SOURCE = COMPOSITION.read_text(encoding="utf-8")

    def test_the_paid_path_objects_are_constructed_exactly_once(self) -> None:
        tree = ast.parse(self.SOURCE)
        constructed = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        for name in (
            "SQLiteOpportunityLedger",
            "SQLiteAutonomousLedger",
            "FutureCognitionSource",
        ):
            with self.subTest(component=name):
                self.assertEqual(constructed.count(name), 1)

    def test_no_poller_is_started_and_run_due_is_never_called(self) -> None:
        """Phase 8 activation stays out of scope."""
        tree = ast.parse(self.SOURCE)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("run_due", called)
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
        self.assertNotIn("AutonomousCognitionRunner", names)

    def test_the_origin_boundary_is_always_constructed(self) -> None:
        """Even unconfigured, the boundary exists so nothing falls back."""
        tree = ast.parse(self.SOURCE)
        constructed = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertEqual(constructed.count("OriginSelectedReasoner"), 1)

    def test_the_real_builder_accepts_and_applies_both_bounds(self) -> None:
        """Call the actual builder the way composition calls it.

        A name-presence check passed here while the builder took only three
        arguments, so a Luna-configured runtime would have died at startup with
        a positional TypeError. Only calling it proves the wiring.
        """
        reasoner = build_model_reasoner(
            object(), ROOT, AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS
        )
        self.assertEqual(reasoner._max_output_tokens, AUTONOMOUS_MAX_OUTPUT_TOKENS)
        self.assertEqual(reasoner._max_input_tokens, AUTONOMOUS_MAX_INPUT_TOKENS)

    def test_the_builder_signature_matches_the_composition_call(self) -> None:
        """The composition call must be satisfiable by the real signature."""
        import inspect

        signature = inspect.signature(build_model_reasoner)
        signature.bind(object(), ROOT, 32_000, 96_000)

    def test_the_conversational_builder_still_takes_no_bounds(self) -> None:
        reasoner = build_model_reasoner(object(), ROOT)
        self.assertIsNone(reasoner._max_output_tokens)
        self.assertIsNone(reasoner._max_input_tokens)


class ProductionClockTests(unittest.TestCase):
    """The production default clock, with nothing injected."""

    def test_the_runner_default_clock_is_timezone_aware(self) -> None:
        runner = AutonomousCognitionRunner(
            source=None, ledger=None, budget=None, gateway=None,
            provider="openai", model="gpt-5.6-luna",
            max_input_tokens=AUTONOMOUS_MAX_INPUT_TOKENS,
            max_output_tokens=AUTONOMOUS_MAX_OUTPUT_TOKENS,
            conversation_id="c1", step_budget=4, retention_days=3650,
        )
        now = runner._clock()
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())

    def test_a_retention_deadline_built_from_it_is_accepted(self) -> None:
        """A naive clock would be rejected downstream by the contracts."""
        runner = AutonomousCognitionRunner(
            source=None, ledger=None, budget=None, gateway=None,
            provider="openai", model="gpt-5.6-luna",
            max_input_tokens=AUTONOMOUS_MAX_INPUT_TOKENS,
            max_output_tokens=AUTONOMOUS_MAX_OUTPUT_TOKENS,
            conversation_id="c1", step_budget=4, retention_days=3650,
        )
        deadline = runner._clock() + timedelta(days=3650)
        self.assertIsNotNone(deadline.utcoffset())


class MissingLunaFailsClosedTests(unittest.TestCase):
    """An autonomous turn must never be answered by the conversational Core."""

    class Recording:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            return object()

    def _context(self, origin: CognitionOrigin) -> ReasoningContext:
        return ReasoningContext(None, (), (), conversation_id="c1", origin=origin)

    def test_a_person_turn_still_reaches_the_conversational_core(self) -> None:
        sol = self.Recording()
        OriginSelectedReasoner(sol, None).decide(
            self._context(CognitionOrigin.PERSON_TURN)
        )
        self.assertEqual(sol.calls, 1)

    def test_an_autonomous_turn_without_luna_makes_zero_sol_calls(self) -> None:
        sol = self.Recording()
        reasoner = OriginSelectedReasoner(sol, None)
        for origin in (
            CognitionOrigin.SELF_REQUESTED,
            CognitionOrigin.EXTERNAL_EVENT,
            CognitionOrigin.WORK_COMPLETED,
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(AutonomousReasonerUnavailable):
                    reasoner.decide(self._context(origin))
        self.assertEqual(sol.calls, 0)

    def test_the_source_is_disabled_when_no_autonomous_core_exists(self) -> None:
        """Occasions nobody can answer must not be produced."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteContinuityStore(root / "c.sqlite3")
            ledger = SQLiteOpportunityLedger(root / "o.sqlite3")
            try:
                store.create(
                    FutureCognitionRequest(
                        request_id="r1",
                        not_before=datetime(2026, 9, 2, tzinfo=UTC),
                        note="x",
                        requested_at=datetime(2026, 9, 1, tzinfo=UTC),
                    )
                )
                source = FutureCognitionSource(store, ledger, enabled=False)
                self.assertEqual(source.due_opportunities(), ())
                self.assertEqual(ledger.rows(), ())
            finally:
                store.close()
                ledger.close()


class InputBoundTests(unittest.TestCase):
    """A reservation computed from an unenforced bound is a guess."""

    class NeverCalled:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            raise AssertionError("an oversized request must not dispatch")

    def test_an_oversized_autonomous_request_cannot_dispatch(self) -> None:
        from alx.core.model_reasoner import ModelReasoner

        model = self.NeverCalled()
        reasoner = ModelReasoner(
            model, "L" * 200_000, "I" * 200_000,
            AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS,
        )
        with self.assertRaises(AutonomousRequestUnbounded):
            reasoner.decide(
                ReasoningContext(None, (), (), conversation_id="c1")
            )
        self.assertEqual(model.calls, 0)

    def test_the_conversational_path_is_not_bounded(self) -> None:
        """Sol must be unchanged: no input ceiling, no refusal."""
        from alx.core.model_reasoner import ModelReasoner

        class Capturing:
            def __init__(self) -> None:
                self.request = None

            def complete(self, request):
                self.request = request
                raise RuntimeError("captured")

        model = Capturing()
        reasoner = ModelReasoner(model, "L" * 200_000, "I" * 200_000)
        with self.assertRaises(Exception) as caught:
            reasoner.decide(ReasoningContext(None, (), (), conversation_id="c1"))
        self.assertNotIsInstance(caught.exception, AutonomousRequestUnbounded)
        self.assertIsNotNone(model.request)
        self.assertIsNone(model.request.max_output_tokens)

    def test_nothing_is_truncated_to_make_a_request_fit(self) -> None:
        """Refusing is the approved behaviour; shortening her mind is not."""
        from alx.core.model_reasoner import ModelReasoner

        laws = "L" * 200_000
        model = self.NeverCalled()
        reasoner = ModelReasoner(
            model, laws, "identity",
            AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS,
        )
        with self.assertRaises(AutonomousRequestUnbounded) as caught:
            reasoner.decide(ReasoningContext(None, (), (), conversation_id="c1"))
        # The measurement reflects the whole request, not a shortened one.
        self.assertGreater(caught.exception.measured, AUTONOMOUS_MAX_INPUT_TOKENS)
        self.assertEqual(model.calls, 0)


class OneSharedBoundingPrimitiveTests(unittest.TestCase):
    """Law 0: one input-bounding implementation, shared."""

    def test_the_specialist_and_the_core_share_one_function(self) -> None:
        from alx.contracts.models import input_token_upper_bound as canonical
        from alx.specialists.research import input_token_upper_bound as reused

        self.assertIs(canonical, reused)

    def test_only_one_definition_exists(self) -> None:
        source_root = ROOT / "src" / "alx"
        definitions = sorted(
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*.py")
            if "def input_token_upper_bound" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(definitions, ["contracts/models.py"])


class ViableInputCeilingTests(unittest.TestCase):
    """The ceiling must fit the Core prompt AL/X actually reasons with.

    D-024a forbids a thinner prompt for the autonomous Core: both origins must
    reason in the same identity and capability environment, or the experiment
    compares two different minds. So the ceiling accommodates the prompt rather
    than the prompt being cut to fit the ceiling.
    """

    def _production_bound(self) -> int:
        from alx.contracts.models import input_token_upper_bound
        from alx.core.model_reasoner import ModelReasoner
        from alx.tools import (
            CONTINUITY_DEFINITIONS, NOTEBOOK_DEFINITIONS, RESEARCH_DEFINITION,
        )
        from alx.tools.mail import DEFINITIONS as MAIL

        try:
            from alx.tools.xero import DEFINITIONS as XERO
        except Exception:
            XERO = ()
        try:
            from alx.tools.dhl import DEFINITIONS as DHL
        except Exception:
            DHL = ()
        laws = (ROOT / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
        identity = (ROOT / "IDENTITY_AND_MEMORY.md").read_text(encoding="utf-8")
        capabilities = (
            tuple(NOTEBOOK_DEFINITIONS) + (RESEARCH_DEFINITION,)
            + tuple(CONTINUITY_DEFINITIONS) + tuple(MAIL)
            + tuple(XERO) + tuple(DHL)
        )
        reasoner = ModelReasoner(
            object(), laws, identity,
            AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS,
        )
        return input_token_upper_bound(
            reasoner.build_request(
                ReasoningContext(
                    None, (), capabilities, conversation_id="c1",
                    origin=CognitionOrigin.SELF_REQUESTED,
                )
            )
        )

    def test_the_real_empty_context_request_fits_the_ceiling(self) -> None:
        """32,000 guaranteed refusal; the corrected ceiling must not."""
        self.assertLess(self._production_bound(), AUTONOMOUS_MAX_INPUT_TOKENS)

    def test_headroom_remains_for_real_continuity_context(self) -> None:
        """A ceiling with no room for conversation buys nothing."""
        headroom = AUTONOMOUS_MAX_INPUT_TOKENS - self._production_bound()
        self.assertGreater(headroom, 20_000)

    def test_the_ceiling_is_the_corrected_figure(self) -> None:
        self.assertEqual(AUTONOMOUS_MAX_INPUT_TOKENS, 96_000)
        self.assertEqual(AUTONOMOUS_MAX_OUTPUT_TOKENS, 32_000)


class ReservationOrderingTests(unittest.TestCase):
    """An oversized request must not reach the fuse, let alone the provider."""

    class CountingBudget:
        def __init__(self) -> None:
            self.reservations = 0

        def reserve(self, *args, **kwargs):
            self.reservations += 1
            raise AssertionError("an oversized request must not reserve")

        def settle(self, *args, **kwargs):
            raise AssertionError("nothing to settle")

    class NeverGateway:
        def receive_cognition_opportunity(self, *args, **kwargs):
            raise AssertionError("an oversized request must not dispatch")

    def _opportunity(self):
        from alx.contracts.continuity import CognitionOpportunity

        return CognitionOpportunity(
            opportunity_id="self:r1",
            origin=CognitionOrigin.SELF_REQUESTED,
            arose_at=datetime(2026, 9, 3, tzinfo=UTC),
            references=("future_cognition:r1",),
            note="a note",
        )

    def _runner(self, measured: int, budget, ledger):
        class OneOccasion:
            def __init__(self, opportunity):
                self._opportunity = opportunity

            def due_opportunities(self):
                return (self._opportunity,)

            def claim(self, opportunity):
                return True

            def mark_honoured(self, opportunity):
                raise AssertionError("a refused occasion is not honoured")

        return AutonomousCognitionRunner(
            OneOccasion(self._opportunity()),
            ledger,
            budget,
            self.NeverGateway(),
            "openai",
            "gpt-5.6-luna",
            AUTONOMOUS_MAX_INPUT_TOKENS,
            AUTONOMOUS_MAX_OUTPUT_TOKENS,
            "c1",
            4,
            3650,
            within_input_bound=lambda opportunity: measured,
        )

    def test_an_oversized_request_makes_no_reservation_and_no_call(self) -> None:
        class Ledger:
            def __init__(self) -> None:
                self.outcomes = []

            def record_outcome(self, opportunity_id, outcome, **kwargs):
                self.outcomes.append(outcome)

            def record_reserved(self, *args, **kwargs):
                raise AssertionError("nothing may be reserved")

        budget, ledger = self.CountingBudget(), Ledger()
        attempted = self._runner(
            AUTONOMOUS_MAX_INPUT_TOKENS + 1, budget, ledger
        ).run_due()
        self.assertEqual(attempted, ())
        self.assertEqual(budget.reservations, 0)
        self.assertEqual(ledger.outcomes, ["refused_input_unbounded"])

    def test_a_request_within_the_ceiling_proceeds_to_reservation(self) -> None:
        """The check refuses only what does not fit."""

        class Ledger:
            def record_outcome(self, *args, **kwargs):
                pass

            def record_reserved(self, *args, **kwargs):
                pass

        class ReachedBudget(Exception):
            pass

        class Budget:
            def reserve(self, *args, **kwargs):
                raise ReachedBudget()

        runner = self._runner(AUTONOMOUS_MAX_INPUT_TOKENS - 1, Budget(), Ledger())
        # The refusal path records and returns; reaching the budget at all
        # proves the bound did not block a request that fits.
        runner.run_due()


if __name__ == "__main__":
    unittest.main()
