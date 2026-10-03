"""The autonomous input bound follows the Core's context window.

On 2026-09-28 the first live activation on the subscription Core refused every
autonomous occasion. Real requests measured 102,899 to 113,387 on the byte
count `input_token_upper_bound` uses, and the ceiling was 96,000, a figure
sized for the retired Luna arrangement. Each refused occasion's claim was
released, so the next tick rebuilt and refused all fourteen again, every thirty
seconds.

These tests hold both parts closed:

- the ceiling is derived from the one recorded context window of the Core's
  model, less a reserve for its output, and it admits those real requests
  while still refusing, without truncation, one that is genuinely too large;
- a refused occasion is held durably instead of released, is not rebuilt on
  every tick, and becomes eligible again when the bound changes or its hold
  lapses, running exactly once.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.autonomous import (  # noqa: E402
    DEFERRED_INPUT_BOUND,
    INPUT_BOUND_RETRY_SECONDS,
    AutonomousCognitionRunner,
    InputBoundHolds,
)
from alx.bootstrap.reasoning import autonomous_input_ceiling  # noqa: E402
from alx.config import (  # noqa: E402
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
    ConfigurationError,
)
from alx.config.settings import AUTONOMOUS_APPROVED_IDENTITY  # noqa: E402
from alx.continuity import (  # noqa: E402
    CompletedWorkSource,
    DueCognitionSource,
    FutureCognitionSource,
    SQLiteContinuityStore,
    SQLiteOpportunityLedger,
)
from alx.continuity.tasks import SQLiteTaskStore  # noqa: E402
from alx.contracts import (  # noqa: E402
    AutonomousRequestUnbounded,
    CognitionOrigin,
    ConversationSnapshot,
    ReasoningContext,
)
from alx.contracts.continuity import (  # noqa: E402
    FutureCognitionRequest,
    FutureCognitionStatus,
)
from alx.contracts.models import (  # noqa: E402
    CORE_MODEL_LIMITS,
    CoreModelLimits,
    core_input_ceiling,
    input_token_upper_bound,
)
from alx.contracts.task import ExternalTask, TaskState  # noqa: E402
from alx.core import CoreAgent  # noqa: E402
from alx.core.loop import INPUT_BOUND_EXCEEDED  # noqa: E402
from alx.core.model_reasoner import ModelReasoner  # noqa: E402

SUBSCRIPTION = AUTONOMOUS_APPROVED_IDENTITY
CEILING = autonomous_input_ceiling(*SUBSCRIPTION)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
# The smallest and largest autonomous requests measured live on 2026-09-28.
LIVE_SMALLEST, LIVE_LARGEST = 102_899, 113_387


class Dispatched(Exception):
    """The model was reached: the request passed the bound and the reservation."""


class Model:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise Dispatched()


class Authority:
    def __init__(self) -> None:
        self.reservations: list[tuple[int, int]] = []

    def reserve(self, max_input_tokens, max_output_tokens):
        self.reservations.append((max_input_tokens, max_output_tokens))
        return "reservation"

    def mark_dispatched(self, reservation):
        pass

    def settle(self, reservation, usage):
        return 0.0


def _context() -> ReasoningContext:
    return ReasoningContext(
        None, (), (), conversation_id="c1", origin=CognitionOrigin.SELF_REQUESTED
    )


def _reasoner_measuring(target: int):
    """A bounded autonomous reasoner whose request measures exactly `target`."""
    base = ModelReasoner(Model(), "L", "identity", AUTONOMOUS_MAX_OUTPUT_TOKENS,
                         CEILING, Authority())
    padding = target - input_token_upper_bound(base.build_request(_context()))
    model, authority = Model(), Authority()
    reasoner = ModelReasoner(model, "L" * (1 + padding), "identity",
                             AUTONOMOUS_MAX_OUTPUT_TOKENS, CEILING, authority)
    assert input_token_upper_bound(reasoner.build_request(_context())) == target
    return reasoner, model, authority


class DerivedCeilingTests(unittest.TestCase):
    def test_the_recorded_limits_are_the_transport_s_reported_ones(self) -> None:
        """Claude Code CLI 2.1.281, exact production invocation, 2026-09-28."""
        self.assertEqual(
            CORE_MODEL_LIMITS[SUBSCRIPTION],
            CoreModelLimits(context_window=1_000_000, max_output=128_000),
        )

    def test_the_ceiling_is_the_window_less_the_maximum_output(self) -> None:
        limits = CORE_MODEL_LIMITS[SUBSCRIPTION]
        self.assertEqual(CEILING, limits.context_window - limits.max_output)
        self.assertEqual(CEILING, 872_000)

    def test_the_reserve_is_the_model_s_output_allowance_not_the_budget(self) -> None:
        """The whole answer the transport permits fits beside a full request."""
        limits = CORE_MODEL_LIMITS[SUBSCRIPTION]
        self.assertEqual(limits.context_window - CEILING, 128_000)
        self.assertNotEqual(limits.context_window - CEILING, AUTONOMOUS_MAX_OUTPUT_TOKENS)

    def test_limits_leaving_no_room_bound_nothing(self) -> None:
        import alx.contracts.models as models

        original = dict(models.CORE_MODEL_LIMITS)
        self.addCleanup(models.CORE_MODEL_LIMITS.update, original)
        models.CORE_MODEL_LIMITS[("p", "m")] = CoreModelLimits(128_000, 128_000)
        self.addCleanup(models.CORE_MODEL_LIMITS.pop, ("p", "m"), None)
        self.assertIsNone(core_input_ceiling("p", "m"))

    def test_the_approved_autonomous_core_has_recorded_limits(self) -> None:
        """Autonomy cannot be switched on for a Core it cannot bound."""
        self.assertIn(SUBSCRIPTION, CORE_MODEL_LIMITS)

    def test_a_model_with_no_recorded_limits_refuses_autonomy(self) -> None:
        self.assertIsNone(core_input_ceiling("claude_subscription", "unknown"))
        with self.assertRaises(ConfigurationError):
            autonomous_input_ceiling("claude_subscription", "unknown")

    def test_the_bound_comes_from_the_conversational_core_s_identity(self) -> None:
        """Composition derives it from the Core that answers Friedl."""
        source = (ROOT / "src" / "alx" / "bootstrap" / "live_voice.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "autonomous_input_ceiling(\n"
            "            provider_settings.reasoning.provider,\n"
            "            provider_settings.reasoning.model,\n",
            source,
        )

    def test_the_luna_figure_is_not_production_policy(self) -> None:
        import alx.config as config

        self.assertFalse(hasattr(config, "AUTONOMOUS_MAX_INPUT_TOKENS"))
        self.assertNotEqual(CEILING, 96_000)
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src").rglob("*.py")
            if "96_000" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class RealisticRequestsFitTests(unittest.TestCase):
    def test_requests_above_the_old_luna_ceiling_are_dispatched(self) -> None:
        for size in (96_001, LIVE_SMALLEST, LIVE_LARGEST, CEILING):
            with self.subTest(size=size):
                reasoner, model, authority = _reasoner_measuring(size)
                with self.assertRaises(Dispatched):
                    reasoner.decide(_context())
                self.assertEqual(model.calls, 1)
                self.assertEqual(
                    authority.reservations, [(CEILING, AUTONOMOUS_MAX_OUTPUT_TOKENS)]
                )

    def test_the_live_range_leaves_real_headroom(self) -> None:
        self.assertGreater(CEILING - LIVE_LARGEST, 700_000)

    def test_a_genuinely_oversized_request_is_refused_untruncated(self) -> None:
        reasoner, model, authority = _reasoner_measuring(CEILING + 1)
        with self.assertRaises(AutonomousRequestUnbounded) as caught:
            reasoner.decide(_context())
        # Measured on the whole request, not on a shortened one, and nothing
        # was reserved or sent.
        self.assertEqual(caught.exception.measured, CEILING + 1)
        self.assertEqual(caught.exception.ceiling, CEILING)
        self.assertEqual(model.calls, 0)
        self.assertEqual(authority.reservations, [])


class CoreNamesTheRefusalTests(unittest.TestCase):
    def test_an_oversized_request_is_reported_as_such(self) -> None:
        class Refusing:
            def decide(self, context):
                raise AutonomousRequestUnbounded(CEILING + 1, CEILING)

        class NoGoals:
            def list_unfinished(self, *args, **kwargs):
                return ()

            def list_needing_core(self, *args, **kwargs):
                return ()

        outcome = CoreAgent(NoGoals(), Refusing(), lambda call, state: None, (),
                            clock=lambda: NOW).process(
            ConversationSnapshot("c1", (), 1, NOW + timedelta(days=1)),
            NOW + timedelta(days=1), 4, origin=CognitionOrigin.SELF_REQUESTED,
        )
        self.assertEqual(outcome.reason, INPUT_BOUND_EXCEEDED)


class _State:
    def __init__(self, value: str) -> None:
        self.value = value


class Gateway:
    """Counts Core turns; `oversized` makes each one an input-bound refusal."""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.oversized = True

    def receive_cognition_opportunity(self, conversation_id, opportunity, *rest):
        self.turns.append(opportunity.opportunity_id)
        if self.oversized:
            return type("Outcome", (), {
                "state": _State("error"), "response": None,
                "reason": INPUT_BOUND_EXCEEDED,
            })()
        return type("Outcome", (), {
            "state": _State("finished_silently"), "response": None, "reason": None,
        })()


class HeldOccasionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = NOW
        self.gateway = Gateway()
        self._open()
        self.store.create(FutureCognitionRequest(
            "revisit-a", NOW - timedelta(days=3), "a note", NOW - timedelta(days=3),
            conversation_id="c1",
        ))

    def _open(self) -> None:
        self.store = SQLiteContinuityStore(self.root / "c.sqlite3")
        self.addCleanup(self.store.close)
        self.ledger = SQLiteOpportunityLedger(self.root / "o.sqlite3")
        self.addCleanup(self.ledger.close)

    def tick(self, ceiling: int = CEILING) -> int:
        source = FutureCognitionSource(self.store, self.ledger, enabled=True,
                                       clock=lambda: self.now)
        holds = InputBoundHolds(self.ledger, ceiling, clock=lambda: self.now)
        runner = AutonomousCognitionRunner(source, self.ledger, self.gateway, 4,
                                           365, clock=lambda: self.now,
                                           holds=holds)
        return asyncio.run(DueCognitionSource(
            source, runner, asyncio.Lock(), 30.0, reopen=holds.reopen,
        ).tick())

    def row(self) -> dict | None:
        rows = [row for row in self.ledger.rows()
                if row["opportunity_id"] == "self:revisit-a"]
        return rows[0] if rows else None

    def pending(self) -> list[str]:
        return [item.request_id for item in self.store.pending()]

    def test_an_oversized_occasion_is_held_not_released(self) -> None:
        self.tick()
        self.assertEqual(self.gateway.turns, ["self:revisit-a"])
        self.assertEqual(self.row()["outcome"], f"{DEFERRED_INPUT_BOUND}:{CEILING}")
        # Not honoured, not lost.
        self.assertEqual(self.pending(), ["revisit-a"])

    def test_it_is_not_rebuilt_on_every_tick(self) -> None:
        for _ in range(10):
            self.tick()
            self.now += timedelta(seconds=30)
        self.assertEqual(self.gateway.turns, ["self:revisit-a"])

    def test_the_hold_survives_restart(self) -> None:
        self.tick()
        self._open()
        # Recovery reclaims only unfinished claims; a hold is not one.
        source = FutureCognitionSource(self.store, self.ledger, enabled=True,
                                       clock=lambda: self.now)
        self.assertEqual(source.recover(), ())
        self.assertEqual(source.due_opportunities(), ())
        self.tick()
        self.assertEqual(self.gateway.turns, ["self:revisit-a"])
        self.assertEqual(self.pending(), ["revisit-a"])

    def test_a_changed_bound_makes_it_eligible_and_it_runs_once(self) -> None:
        self.tick()
        self.gateway.oversized = False
        self.tick(ceiling=CEILING + 10_000)
        self.assertEqual(self.gateway.turns, ["self:revisit-a", "self:revisit-a"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.row()["outcome"], "finished_silently")
        # Honoured: no further turn at any bound.
        self.tick(ceiling=CEILING + 20_000)
        self.assertEqual(len(self.gateway.turns), 2)

    def test_a_lapsed_hold_is_tried_again_once(self) -> None:
        self.tick()
        self.now += timedelta(seconds=INPUT_BOUND_RETRY_SECONDS - 1)
        self.tick()
        self.assertEqual(len(self.gateway.turns), 1)
        self.now += timedelta(seconds=1)
        self.tick()
        self.assertEqual(len(self.gateway.turns), 2)
        # Still oversized, so held again from now, not released.
        self.assertEqual(self.row()["outcome"], f"{DEFERRED_INPUT_BOUND}:{CEILING}")
        self.assertEqual(self.pending(), ["revisit-a"])

    def test_the_hold_is_recorded_when_it_was_made(self) -> None:
        self.tick()
        self.assertEqual(self.row()["recorded_at"], NOW.isoformat())


class HeldCompletedWorkTests(unittest.TestCase):
    """A finished review too large to think about is held, not handed over."""

    def test_the_task_stays_unhandled_until_it_is_thought_about(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        tasks = SQLiteTaskStore(root / "tasks.sqlite3")
        tasks.record(ExternalTask(
            task_id="review:72:x", kind="external_review", service="coderabbit",
            subject_reference="pull/72@" + "a" * 40, state=TaskState.COMPLETED,
            requested_at=NOW - timedelta(hours=1), completed_at=NOW,
            conversation_id="c1",
        ))
        ledger = SQLiteOpportunityLedger(root / "o.sqlite3")
        self.addCleanup(ledger.close)
        source = CompletedWorkSource(tasks, ledger, enabled=True)
        gateway = Gateway()
        holds = InputBoundHolds(ledger, CEILING, clock=lambda: NOW)
        runner = AutonomousCognitionRunner(source, ledger, gateway, 4, 365,
                                           clock=lambda: NOW, holds=holds)
        tick = DueCognitionSource(source, runner, asyncio.Lock(), 30.0,
                                  reopen=holds.reopen).tick
        asyncio.run(tick())
        asyncio.run(tick())
        self.assertEqual(gateway.turns, ["task:review:72:x"])
        self.assertEqual(len(tasks.completed_unhandled()), 1)

        gateway.oversized = False
        bigger = InputBoundHolds(ledger, CEILING + 1, clock=lambda: NOW)
        runner = AutonomousCognitionRunner(source, ledger, gateway, 4, 365,
                                           clock=lambda: NOW, holds=bigger)
        asyncio.run(DueCognitionSource(source, runner, asyncio.Lock(), 30.0,
                                       reopen=bigger.reopen).tick())
        self.assertEqual(len(gateway.turns), 2)
        self.assertEqual(tasks.completed_unhandled(), ())


if __name__ == "__main__":
    unittest.main()
