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


class RecordingAuthority:
    """A real spend authority that records what it was asked to do."""

    def __init__(self, reservation: str = "res-1") -> None:
        self.reservations: list[tuple[int, int]] = []
        self.settlements: list = []
        self._reservation = reservation

    def reserve(self, max_input_tokens: int, max_output_tokens: int):
        self.reservations.append((max_input_tokens, max_output_tokens))
        return self._reservation

    def settle(self, reservation, usage):
        self.settlements.append((reservation, usage))
        return 0.0


def _bounded_reasoner(model, laws="laws", identity="identity", authority=None):
    """An autonomous reasoner exactly as composition builds one."""
    from alx.core.model_reasoner import ModelReasoner

    return ModelReasoner(
        model, laws, identity,
        AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS,
        authority or RecordingAuthority(),
    )


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
        """EX-001: the boundary exists even unconfigured, so nothing falls back."""
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
            object(), ROOT, AUTONOMOUS_MAX_OUTPUT_TOKENS,
            AUTONOMOUS_MAX_INPUT_TOKENS, RecordingAuthority(),
        )
        self.assertEqual(reasoner._max_output_tokens, AUTONOMOUS_MAX_OUTPUT_TOKENS)
        self.assertEqual(reasoner._max_input_tokens, AUTONOMOUS_MAX_INPUT_TOKENS)

    def test_the_builder_signature_matches_the_composition_call(self) -> None:
        """The composition call must be satisfiable by the real signature."""
        import inspect

        signature = inspect.signature(build_model_reasoner)
        signature.bind(object(), ROOT, 32_000, 96_000, RecordingAuthority())

    def test_the_conversational_builder_still_takes_no_bounds(self) -> None:
        reasoner = build_model_reasoner(object(), ROOT)
        self.assertIsNone(reasoner._max_output_tokens)
        self.assertIsNone(reasoner._max_input_tokens)


class ProductionClockTests(unittest.TestCase):
    """The production default clock, with nothing injected."""

    def test_the_runner_default_clock_is_timezone_aware(self) -> None:
        runner = AutonomousCognitionRunner(
            source=None, ledger=None, gateway=None,
            conversation_id="c1", step_budget=4, retention_days=3650,
        )
        now = runner._clock()
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())

    def test_a_retention_deadline_built_from_it_is_accepted(self) -> None:
        """A naive clock would be rejected downstream by the contracts."""
        runner = AutonomousCognitionRunner(
            source=None, ledger=None, gateway=None,
            conversation_id="c1", step_budget=4, retention_days=3650,
        )
        deadline = runner._clock() + timedelta(days=3650)
        self.assertIsNotNone(deadline.utcoffset())



class MissingLunaFailsClosedTests(unittest.TestCase):
    """An autonomous turn must never be answered by the conversational Core.

    EX-001 prohibits a fallback: answering with Sol would spend on a Core
    nobody selected and record the result as if the experiment had run.
    """

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
        reasoner = _bounded_reasoner(model, "L" * 200_000, "I" * 200_000)
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
        reasoner = _bounded_reasoner(model, laws, "identity")
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
        reasoner = _bounded_reasoner(object(), laws, identity)
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


class SameRequestObjectTests(unittest.TestCase):
    """Measured, authorised and dispatched must be one object.

    The earlier shape measured an estimate in the runner and let the reasoner
    build a different request afterwards, so the ceiling was checked against a
    request that was never sent and the reservation was already spent by the
    time the real one was built.
    """

    class Capturing:
        def __init__(self) -> None:
            self.request = None

        def complete(self, request):
            self.request = request

            class Completion:
                output = {
                    "action": {"type": "finish_silently"},
                    "goal_id": None,
                    "goal_update": None,
                    "memory_proposals": [],
                }
                usage = {"input_tokens": 10, "output_tokens": 1}

            return Completion()

    def test_the_dispatched_request_is_the_measured_one(self) -> None:
        from alx.contracts.models import input_token_upper_bound

        authority = RecordingAuthority()
        model = self.Capturing()
        reasoner = _bounded_reasoner(model, authority=authority)
        context = ReasoningContext(
            None, (), (), conversation_id="c1",
            origin=CognitionOrigin.SELF_REQUESTED,
        )
        expected = reasoner.build_request(context)
        # The decision payload shape is incidental here; what matters is which
        # request object reached the provider and whether it was authorised.
        try:
            reasoner.decide(context)
        except Exception:
            pass
        sent = model.request
        self.assertIsNotNone(sent)
        # Same content, and small enough that it was authorised rather than
        # refused: one construction served measurement and dispatch.
        self.assertEqual(
            [item.content for item in sent.messages],
            [item.content for item in expected.messages],
        )
        self.assertEqual(
            input_token_upper_bound(sent), input_token_upper_bound(expected)
        )
        self.assertEqual(len(authority.reservations), 1)

    def test_reservation_precedes_dispatch_for_the_same_request(self) -> None:
        order: list[str] = []

        class Ordered(RecordingAuthority):
            def reserve(inner, max_input_tokens, max_output_tokens):
                order.append("reserved")
                return super().reserve(max_input_tokens, max_output_tokens)

            def settle(inner, reservation, usage):
                order.append("settled")
                return super().settle(reservation, usage)

        class Model(SameRequestObjectTests.Capturing):
            def complete(inner, request):
                order.append("dispatched")
                return super().complete(request)

        reasoner = _bounded_reasoner(Model(), authority=Ordered())
        try:
            reasoner.decide(
                ReasoningContext(None, (), (), conversation_id="c1",
                                 origin=CognitionOrigin.SELF_REQUESTED)
            )
        except Exception:
            pass
        self.assertEqual(order, ["reserved", "dispatched", "settled"])

    def test_an_oversized_real_request_never_reserves(self) -> None:
        """No estimate: the real request is measured and refused."""

        class NeverCalled:
            def complete(self, request):
                raise AssertionError("an oversized request must not dispatch")

        authority = RecordingAuthority()
        reasoner = _bounded_reasoner(
            NeverCalled(), "L" * 400_000, "I" * 400_000, authority
        )
        with self.assertRaises(Exception) as caught:
            reasoner.decide(ReasoningContext(None, (), (), conversation_id="c1"))
        self.assertIn("above the", str(caught.exception))
        self.assertEqual(authority.reservations, [])
        self.assertEqual(authority.settlements, [])

    def test_a_failed_dispatch_still_settles_its_reservation(self) -> None:
        class Exploding:
            def complete(self, request):
                raise RuntimeError("provider exploded")

        authority = RecordingAuthority()
        reasoner = _bounded_reasoner(Exploding(), authority=authority)
        with self.assertRaises(Exception):
            reasoner.decide(
                ReasoningContext(None, (), (), conversation_id="c1")
            )
        self.assertEqual(len(authority.reservations), 1)
        self.assertEqual(len(authority.settlements), 1)
        # Usage unknown, so the reservation stands rather than returning.
        self.assertIsNone(authority.settlements[0][1])


class MandatoryIntegrationTests(unittest.TestCase):
    """A bounded reasoner without a budget must not be constructible."""

    def test_a_bound_without_a_spend_authority_is_refused(self) -> None:
        from alx.core.model_reasoner import ModelReasoner

        with self.assertRaises(ValueError):
            ModelReasoner(object(), "laws", "identity", 32_000, 96_000, None)

    def test_a_spend_authority_without_a_bound_is_refused(self) -> None:
        from alx.core.model_reasoner import ModelReasoner

        with self.assertRaises(ValueError):
            ModelReasoner(
                object(), "laws", "identity", 32_000, None, RecordingAuthority()
            )

    def test_the_conversational_reasoner_needs_neither(self) -> None:
        from alx.core.model_reasoner import ModelReasoner

        reasoner = ModelReasoner(object(), "laws", "identity")
        self.assertIsNone(reasoner._max_input_tokens)
        self.assertIsNone(reasoner._spend_authority)

    def test_the_runner_has_no_optional_bound_check_left(self) -> None:
        """No code path may skip the ceiling."""
        source = (
            ROOT / "src" / "alx" / "bootstrap" / "autonomous.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("within_input_bound", source)


class RequestObjectIdentityTests(unittest.TestCase):
    """The checked object and the dispatched object must be identical.

    Equality is not enough. Two separately constructed requests with the same
    content would compare equal while still meaning the ceiling was checked
    against a request that was never sent, so this asserts `is`.
    """

    def _trace(self, laws="laws", identity="identity"):
        """Run one autonomous turn, recording every object and its order."""
        import alx.core.model_reasoner as module

        events: list[tuple[str, int | None]] = []

        class Authority:
            def reserve(inner, max_input_tokens, max_output_tokens):
                events.append(("reserve", None))
                return "reservation"

            def settle(inner, reservation, usage):
                events.append(("settle", None))
                return 0.0

        class Model:
            def complete(inner, request):
                events.append(("complete", id(request)))
                inner.dispatched = request
                raise RuntimeError("captured after dispatch")

        model = Model()
        model.dispatched = None
        original = module.input_token_upper_bound
        checked: list = []

        def spy(request):
            events.append(("measure", id(request)))
            checked.append(request)
            return original(request)

        module.input_token_upper_bound = spy
        try:
            reasoner = module.ModelReasoner(
                model, laws, identity,
                AUTONOMOUS_MAX_OUTPUT_TOKENS, AUTONOMOUS_MAX_INPUT_TOKENS,
                Authority(),
            )
            try:
                reasoner.decide(
                    ReasoningContext(
                        None, (), (), conversation_id="c1",
                        origin=CognitionOrigin.SELF_REQUESTED,
                    )
                )
            except Exception:
                pass
        finally:
            module.input_token_upper_bound = original
        return events, checked, model.dispatched

    def test_the_checked_request_is_the_dispatched_request(self) -> None:
        _, checked, dispatched = self._trace()
        self.assertEqual(len(checked), 1, "exactly one measurement")
        self.assertIsNotNone(dispatched)
        self.assertIs(checked[0], dispatched)

    def test_reserve_happens_after_construction_and_after_validation(self) -> None:
        events, _, _ = self._trace()
        order = [name for name, _ in events]
        self.assertEqual(order, ["measure", "reserve", "complete", "settle"])

    def test_only_one_request_is_ever_constructed(self) -> None:
        """A rebuild after reservation would show a second distinct object."""
        events, _, _ = self._trace()
        object_ids = {ident for _, ident in events if ident is not None}
        self.assertEqual(len(object_ids), 1)

    def test_an_oversized_real_request_reserves_nothing(self) -> None:
        """Measured from the real build_request output, not an estimate."""
        events, checked, dispatched = self._trace("L" * 400_000, "I" * 400_000)
        self.assertEqual([name for name, _ in events], ["measure"])
        self.assertIsNone(dispatched)
        self.assertEqual(len(checked), 1)


class NoOptionalBoundPathTests(unittest.TestCase):
    """No production path may skip the ceiling."""

    def test_the_runner_takes_no_bound_measuring_argument(self) -> None:
        import inspect

        parameters = inspect.signature(AutonomousCognitionRunner).parameters
        for name in parameters:
            with self.subTest(parameter=name):
                self.assertNotIn("bound", name)

    def test_the_runner_never_reserves(self) -> None:
        """Reservation lives at the boundary holding the request, not here."""
        import inspect

        source = inspect.getsource(AutonomousCognitionRunner)
        self.assertNotIn(".reserve(", source)
        self.assertNotIn(".settle(", source)


class OccasionCostIsRecordedTests(unittest.TestCase):
    """D-024 requires every dollar inspectable per occasion, not only per day.

    Moving spend into the reasoning boundary left the opportunity ledger with
    no provider, model or cost, so the record used to judge the experiment
    would have shown which occasions ran but not what any of them cost.
    """

    def test_reserved_and_settled_cost_reach_the_opportunity_row(self) -> None:
        import tempfile
        from alx.bootstrap.autonomous import (
            AutonomousCognitionRunner, LedgerSpendAuthority, OccasionSpendRelay,
        )
        from alx.continuity import SQLiteOpportunityLedger
        from alx.contracts.continuity import CognitionOpportunity

        class Reservation:
            reservation_id = "res-1"
            reserved_usd = 0.0816

        class Ledger:
            def reserve(self, provider, model, max_input, max_output):
                return Reservation()

            def settle(self, reservation, provider, model, usage):
                return 0.0079

        opportunity = CognitionOpportunity(
            "self:r1", CognitionOrigin.SELF_REQUESTED,
            datetime(2026, 9, 3, tzinfo=UTC), ("future_cognition:r1",),
        )

        class Source:
            def due_opportunities(self):
                return (opportunity,)

            def claim(self, item):
                return True

            def mark_honoured(self, item):
                pass

        relay = OccasionSpendRelay()
        authority = LedgerSpendAuthority(
            Ledger(), "openai", "gpt-5.6-luna", relay
        )

        class Gateway:
            def receive_cognition_opportunity(self, *args, **kwargs):
                # Stands in for the reasoning boundary authorising the turn.
                reservation = authority.reserve(96_000, 32_000)
                authority.settle(reservation, {"input_tokens": 1})

                class Outcome:
                    class state:
                        value = "finished_silently"

                return Outcome()

        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLiteOpportunityLedger(Path(directory) / "o.sqlite3")
            try:
                ledger.record_created(opportunity)
                AutonomousCognitionRunner(
                    Source(), ledger, Gateway(), "c1", 4, 3650,
                    spend_observer=relay,
                ).run_due()
                row = ledger.rows()[0]
                self.assertEqual(row["provider"], "openai")
                self.assertEqual(row["model"], "gpt-5.6-luna")
                self.assertAlmostEqual(row["reserved_usd"], 0.0816, places=6)
                self.assertAlmostEqual(row["settled_usd"], 0.0079, places=6)
            finally:
                ledger.close()

    def test_the_relay_is_wired_in_the_composition_root(self) -> None:
        import ast

        tree = ast.parse(COMPOSITION.read_text(encoding="utf-8"))
        constructed = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertEqual(constructed.count("OccasionSpendRelay"), 1)


if __name__ == "__main__":
    unittest.main()
