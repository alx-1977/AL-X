"""D-065: native calls run through the Core's own step loop, in one paused session.

The fake transport pauses twice on native calls and then answers. Each call is
dispatched by `CoreAgent.process` exactly as a decision's call would be, and
the transport is continued with the attempt the Core recorded, not with
anything the transport made up. A refused native call is continued with the
refusal.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, GoalState, GoalSummary, ModelCompletion, Objective, ReasoningContext,
    SideEffect, StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core.loop import CoreAgent, CoreState  # noqa: E402
from alx.core.model_reasoner import ModelReasoner  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402

from tests.test_model_reasoner import base_output  # noqa: E402

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=1)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
READ = CapabilityDefinition("read_note", "Read a note", SCHEMA, SCHEMA, SideEffect.NONE)


def native(call_id: str, capability_id: str = "read_note") -> dict:
    return {"call_id": call_id, "capability_id": capability_id,
            "arguments_json": json.dumps({"id": call_id}), "approval_id": None}


class PausingTransport:
    """Pauses on each scripted native call, then answers with the last output."""

    def __init__(self, script: list, final: dict) -> None:
        self.script = list(script)
        self.final = final
        self.requests = []
        self.abandoned = 0
        self.native_tools_enabled = True

    def complete(self, request):
        self.requests.append(request)
        if self.script:
            return ModelCompletion("fake", "fake", {}, {}, tool_use=self.script.pop(0))
        return ModelCompletion("fake", "fake", self.final, {})

    def abandon_session(self) -> None:
        self.abandoned += 1


def conversation() -> ConversationSnapshot:
    turn = ConversationTurn("c1", "t1", ConversationOrigin.TYPED, "What do my notes say?",
                            NOW, "friedl")
    return ConversationSnapshot("c1", (turn,), 1, RETENTION)


class NativeTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.dispatched = []

    def dispatch(self, call, _state):
        self.dispatched.append(call.call_id)
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.SUCCEEDED,
                             {"text": f"note {call.call_id}"}))

    def run_turn(self, transport):
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity"),
                         self.dispatch, (READ,), clock=lambda: NOW)
        return core.process(conversation(), RETENTION, 8)

    def test_two_native_reads_and_an_answer_in_one_paused_session(self) -> None:
        transport = PausingTransport([native("n1"), native("n2")],
                                     base_output(response="Both notes read."))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Both notes read.")
        self.assertEqual(self.dispatched, ["n1", "n2"])
        first, second, third = transport.requests
        self.assertTrue(first.native_tools)
        self.assertIsNone(first.tool_result)
        # Each continuation carries the attempt the Core recorded for that call.
        self.assertEqual(json.loads(second.tool_result)["call_id"], "n1")
        self.assertEqual(json.loads(second.tool_result)["result_values"], {"text": "note n1"})
        self.assertEqual(json.loads(third.tool_result)["call_id"], "n2")
        self.assertEqual(transport.abandoned, 0)

    def test_a_refused_native_call_continues_with_the_refusal(self) -> None:
        transport = PausingTransport([native("x1", "not_a_capability")],
                                     base_output(response="That tool does not exist."))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, [])
        self.assertIn("refused", json.loads(transport.requests[1].tool_result))

    def test_a_reused_call_id_is_answered_with_its_refusal_not_the_old_result(self) -> None:
        """Review of #197: the earlier success must not answer the new call."""
        second = {**native("n1"), "arguments_json": json.dumps({"id": "other"})}
        transport = PausingTransport([native("n1"), second], base_output(response="ok"))
        self.run_turn(transport)
        self.assertEqual(self.dispatched, ["n1"])
        self.assertIn("refused", json.loads(transport.requests[2].tool_result))

    def test_a_turn_that_starts_fresh_abandons_a_stale_session(self) -> None:
        transport = PausingTransport([native("n1")], base_output(response="done"))
        reasoner = ModelReasoner(transport, "laws", "identity")
        core = CoreAgent(self.store, reasoner, self.dispatch, (READ,), clock=lambda: NOW)
        core.process(conversation(), RETENTION, 8)
        # A paused call from an earlier turn whose outcome this context lacks.
        reasoner._native_pending = ("gone", None, 0, False)
        transport.script = []
        core.process(conversation(), RETENTION, 8)
        self.assertEqual(transport.abandoned, 1)
        self.assertIsNone(transport.requests[-1].tool_result)


class ContinuationBoundaryTests(unittest.TestCase):
    """Review of #197: a session is continued only from its own turn."""

    def setUp(self) -> None:
        call = CapabilityCall("n1", "read_note", {}, None)
        recorded = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("n1", "read_note", CapabilityResultState.SUCCEEDED, {"text": "x"}))
        self.goal = GoalState("g1", Objective("turn:t1", "Read notes"),
                              (SuccessCriterion("read", "read"),), attempts=(recorded,))
        self.transport = PausingTransport([], base_output(response="done"))
        self.reasoner = ModelReasoner(self.transport, "laws", "identity")
        # Paused on n1, whose attempt the goal holds from some turn.
        self.reasoner._native_pending = ("n1", "g1", 0, False)

    def context(self, **changes) -> ReasoningContext:
        return ReasoningContext(self.goal, conversation().turns, (READ,), conversation_id="c1",
                                unfinished_goals=(GoalSummary.of(self.goal),), **changes)

    def test_an_attempt_from_an_earlier_turn_does_not_continue_the_session(self) -> None:
        self.assertFalse(self.reasoner.can_continue(self.context()))
        self.reasoner.decide(self.context())
        self.assertIsNone(self.transport.requests[-1].tool_result)
        self.assertEqual(self.transport.abandoned, 1)

    def test_the_same_attempt_dispatched_in_this_turn_does(self) -> None:
        self.assertTrue(self.reasoner.can_continue(self.context(turn_call_ids=("n1",))))
        self.reasoner.decide(self.context(turn_call_ids=("n1",)))
        self.assertEqual(json.loads(self.transport.requests[-1].tool_result)["call_id"], "n1")

    def test_a_words_only_step_starts_fresh_instead_of_failing(self) -> None:
        self.transport.final = {**base_output(response="Sorry."), "goal_id": None}
        decision = self.reasoner.decide(self.context(turn_call_ids=("n1",),
                                                     response_only_reason="blocked"))
        self.assertEqual(decision.response, "Sorry.")
        self.assertFalse(self.transport.requests[-1].native_tools)
        self.assertEqual(self.transport.abandoned, 1)


if __name__ == "__main__":
    unittest.main()
