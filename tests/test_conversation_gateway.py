from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision,
    ConversationOrigin,
    ConversationTurn,
    GoalStatus,
    GoalStopReason,
    WorkItem,
)
from alx.conversation import ConversationGateway  # noqa: E402
from alx.core import CoreAgent, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.goals import GoalNotFound  # noqa: E402


NOW = datetime(2026, 8, 27, tzinfo=UTC)


class AwaitingReasoner:
    def __init__(self) -> None:
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        waiting = replace(
            context.goal,
            outstanding_work=(WorkItem("next-turn", "continue the conversation"),),
            status=GoalStatus.AWAITING_INPUT,
            stop_reason=GoalStopReason.REQUIRED_INPUT,
        )
        return AgentDecision(waiting, response="authoritative response")


class ConversationGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "goals.sqlite3"
        self.store = SQLiteGoalStore(self.path)
        self.reasoner = AwaitingReasoner()
        core = CoreAgent(self.store, self.reasoner, lambda call, state: None, ())
        self.gateway = ConversationGateway(core)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_typed_and_speech_turns_use_the_same_gateway_and_core(self) -> None:
        typed = ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.TYPED, "First request", NOW
        )
        first = self.gateway.receive(
            typed,
            "goal-1",
            1,
            retention_until=NOW + timedelta(days=30),
        )
        speech = ConversationTurn(
            "conversation-1",
            "turn-2",
            ConversationOrigin.SPEECH_TRANSCRIPT,
            "Actually, change that detail",
            NOW + timedelta(minutes=1),
        )
        second = self.gateway.receive(speech, "goal-1", 1)

        self.assertEqual(first.state, CoreState.RESPONDED)
        self.assertEqual(second.state, CoreState.RESPONDED)
        self.assertEqual(len(self.reasoner.contexts), 2)
        self.assertEqual(self.reasoner.contexts[1].turns, (typed, speech))
        self.assertEqual(self.store.load("goal-1").turns, (typed, speech))

    def test_new_turn_is_durable_before_reasoning_failure(self) -> None:
        class FailingReasoner:
            def decide(self, context):
                raise RuntimeError("provider unavailable")

        core = CoreAgent(self.store, FailingReasoner(), lambda call, state: None, ())
        gateway = ConversationGateway(core)
        speech = ConversationTurn(
            "conversation-2",
            "turn-1",
            ConversationOrigin.SPEECH_TRANSCRIPT,
            "Please remember this request",
            NOW,
        )

        outcome = gateway.receive(
            speech,
            "goal-2",
            1,
            retention_until=NOW + timedelta(days=30),
        )

        self.assertEqual(outcome.reason, "reasoner_error")
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        recovered = self.store.load("goal-2")
        self.assertEqual(recovered.turns, (speech,))
        self.assertEqual(recovered.state.objective.summary, speech.content)

    def test_invalid_budget_does_not_create_or_change_durable_state(self) -> None:
        first = ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.TYPED, "First request", NOW
        )
        with self.assertRaises(ValueError):
            self.gateway.receive(
                first,
                "goal-1",
                0,
                retention_until=NOW + timedelta(days=30),
            )
        with self.assertRaises(GoalNotFound):
            self.store.load("goal-1")

    def test_gateway_alone_owns_durable_goal_selection_for_conversation_turns(self) -> None:
        class RecordingCore:
            def __init__(self):
                self.calls = []

            def start(self, goal_id, turn, retention_until, step_budget):
                self.calls.append(("start", goal_id, turn, retention_until, step_budget))
                return "started"

            def continue_goal(self, goal_id, turn, step_budget):
                self.calls.append(("continue", goal_id, turn, None, step_budget))
                return "continued"

        core = RecordingCore()
        located = iter((None, "goal-1", None))
        identifiers = iter(("goal-1", "goal-2"))
        gateway = ConversationGateway(
            core,
            lambda conversation_id: next(located),
            lambda: next(identifiers),
        )
        turns = tuple(
            ConversationTurn(
                "conversation-1",
                f"turn-{number}",
                ConversationOrigin.SPEECH_TRANSCRIPT,
                f"words-{number}",
                NOW + timedelta(minutes=number),
            )
            for number in range(3)
        )
        retention = NOW + timedelta(days=30)

        results = tuple(
            gateway.receive_conversation_turn(turn, 4, retention) for turn in turns
        )

        self.assertEqual(results, ("started", "continued", "started"))
        self.assertEqual(
            [(call[0], call[1]) for call in core.calls],
            [("start", "goal-1"), ("continue", "goal-1"), ("start", "goal-2")],
        )


if __name__ == "__main__":
    unittest.main()
